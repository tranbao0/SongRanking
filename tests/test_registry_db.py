"""
Guards the registry schema, snapshot.take_snapshot's pending query, and
the git-based data repo sync (pull_to_local/push_from_local) - see
db.py's module docstring for the overall design.

The index assertions are deliberately about query *plans* rather than about
which indexes exist: the point of the change was that snapshot lookups stop
scanning a table that grows by one row per video per day, and a plan is the
only thing that actually states that.
"""

import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from .context import make_db, add_channel, add_video, add_snapshot

from registry import db, snapshot


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _index_names(self, table):
        return {row["name"] for row in self.conn.execute(f"PRAGMA index_list({table})")}

    def test_redundant_video_id_index_is_gone(self):
        """
        view_snapshots' primary key already leads with video_id, so a
        separate index on it could never be chosen - it only cost write
        time on every snapshot insert.
        """
        self.assertNotIn("idx_snapshots_video", self._index_names("view_snapshots"))

    def test_snapshot_date_is_indexed(self):
        self.assertIn("idx_snapshots_date", self._index_names("view_snapshots"))

    def test_pending_snapshot_lookup_does_not_scan_snapshots(self):
        plan = " ".join(
            row["detail"] for row in self.conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT v.video_id, v.url FROM videos v
                WHERE NOT EXISTS (
                    SELECT 1 FROM view_snapshots s
                    WHERE s.video_id = v.video_id AND s.snapshot_date = ?
                )
                """,
                ("2026-08-01",),
            )
        )
        self.assertNotIn("SCAN view_snapshots", plan)
        self.assertNotIn("SCAN s", plan)

    def test_migrations_are_idempotent(self):
        """get_connection() reapplies these on every open."""
        for _ in range(3):
            for migration in db._MIGRATIONS:
                try:
                    self.conn.execute(migration)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
            self.conn.executescript(db._POST_MIGRATION_INDEXES)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(videos)")}
        self.assertIn("song_id", columns)
        self.assertIn("tags", columns)

    def test_dropping_the_old_index_is_safe_on_a_database_that_has_it(self):
        """Existing databases carry idx_snapshots_video and must migrate cleanly."""
        self.conn.execute("CREATE INDEX idx_snapshots_video ON view_snapshots(video_id)")
        self.conn.executescript(db._POST_MIGRATION_INDEXES)
        self.assertNotIn("idx_snapshots_video", self._index_names("view_snapshots"))


class TakeSnapshotPendingTest(unittest.TestCase):
    """
    Exercises take_snapshot against a temporary database, with the YouTube
    fetch stubbed - no API key, no network, no quota consumed.
    """

    def setUp(self):
        # take_snapshot closes the connection it is handed in a finally
        # block, which would drop the in-memory database mid-test.
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        self.today = date.today().isoformat()

        add_channel(self.conn, "UC_a")
        for i in range(3):
            add_video(self.conn, f"v{i}", "UC_a", song_id=i + 1)
        self.conn.commit()

        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_with_views(self, views_by_url):
        def _fake_fetch(urls, on_result=None, **kwargs):
            results = {}
            for url in urls:
                if url not in views_by_url:
                    continue
                meta = {"views": views_by_url[url]}
                results[url] = meta
                if on_result:
                    on_result(url, meta)
            return results

        with mock.patch.object(snapshot, "batch_fetch_metadata", side_effect=_fake_fetch) as fetch:
            inserted = snapshot.take_snapshot()
        return inserted, fetch

    def _urls(self, *video_ids):
        return {f"https://www.youtube.com/watch?v={v}": 100 for v in video_ids}

    def test_inserts_a_row_per_tracked_video(self):
        inserted, _ = self._run_with_views(self._urls("v0", "v1", "v2"))
        self.assertEqual(inserted, 3)

    def test_rerunning_the_same_day_is_a_no_op(self):
        """Idempotence is what makes a re-run after a crash safe."""
        self._run_with_views(self._urls("v0", "v1", "v2"))
        inserted, fetch = self._run_with_views({})
        self.assertEqual(inserted, 0)
        fetch.assert_not_called()  # and costs no quota

    def test_only_videos_still_missing_today_are_fetched(self):
        add_snapshot(self.conn, "v0", self.today, 500)
        self.conn.commit()

        _, fetch = self._run_with_views(self._urls("v1", "v2"))
        requested = set(fetch.call_args[0][0])
        self.assertEqual(requested, set(self._urls("v1", "v2")))

    def test_videos_missing_from_the_fetch_response_are_skipped(self):
        """Deleted/private videos must not insert a bogus zero."""
        inserted, _ = self._run_with_views(self._urls("v0"))
        self.assertEqual(inserted, 1)
        rows = self.conn.execute(
            "SELECT video_id FROM view_snapshots WHERE snapshot_date = ?", (self.today,)
        ).fetchall()
        self.assertEqual([r["video_id"] for r in rows], ["v0"])

    def test_ungrouped_videos_are_never_fetched(self):
        """
        song_id NULL means a real video awaiting (re-)grouping - not yet
        chartable, so it must not cost a fetch until it's grouped.
        """
        add_video(self.conn, "blocked", "UC_a", song_id=None)
        self.conn.commit()

        inserted, fetch = self._run_with_views(self._urls("v0", "v1", "v2", "blocked"))
        self.assertEqual(inserted, 3)
        requested = set(fetch.call_args[0][0])
        self.assertNotIn("https://www.youtube.com/watch?v=blocked", requested)


class LocalBackupTest(unittest.TestCase):
    """
    Guards pull_to_local()'s pre-rebuild backup: a pull replaces the local
    working copy wholesale with the data repo's current state, so without a
    backup, whatever not-yet-pushed local work it clobbers would be
    unrecoverable.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        patcher = mock.patch.object(db, "LOCAL_WORKING_DB_PATH", self.data_dir / ".registry_working.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _backups(self):
        return sorted(self.data_dir.glob("registry.*.pre-pull.db"))

    def test_no_op_when_no_local_file_exists_yet(self):
        db._backup_and_prune_local()
        self.assertEqual(self._backups(), [])

    def test_backs_up_the_existing_local_file(self):
        db.LOCAL_WORKING_DB_PATH.write_text("working copy contents")
        db._backup_and_prune_local()
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "working copy contents")

    def test_keeps_only_the_3_most_recent_backups(self):
        for ts in ("20260101-000000", "20260101-000001", "20260101-000002", "20260101-000003"):
            (self.data_dir / f"registry.{ts}.pre-pull.db").write_text("old")
        db.LOCAL_WORKING_DB_PATH.write_text("current")

        db._backup_and_prune_local()

        names = {p.name for p in self._backups()}
        self.assertEqual(len(names), 3)
        self.assertNotIn("registry.20260101-000000.pre-pull.db", names)
        self.assertNotIn("registry.20260101-000001.pre-pull.db", names)
        self.assertIn("registry.20260101-000003.pre-pull.db", names, "newest pre-seeded backup must survive")


def _init_bare_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True, capture_output=True)


def _rmtree_git_checkout(path: Path) -> None:
    """
    shutil.rmtree() alone can't remove a git checkout on Windows: git
    marks committed objects read-only, which raises PermissionError
    partway through. Clear the read-only bit and retry on that failure.
    """
    def _make_writable_and_retry(func, target, exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_make_writable_and_retry)


class GitSyncTest(unittest.TestCase):
    """
    Exercises pull_to_local()/push_from_local() against a real local git
    repo standing in for the hosted data repo (a bare repo on disk) - real
    `git` subprocess calls, no network. Covers: the full round trip, the
    append-only fast path staying a no-op when nothing changed, and the
    rare-delete fallback to a full chunk-file regeneration (see db.py's
    _needs_full_regen).
    """

    def setUp(self):
        # ignore_cleanup_errors: TemporaryDirectory's own teardown is a
        # plain shutil.rmtree with no read-only handling - see
        # _rmtree_git_checkout, which every test that needs to actually
        # remove the git checkout mid-test uses instead.
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)

        self.remote = root / "remote-data.git"
        _init_bare_repo(self.remote)

        for name, value in (
            ("LOCAL_WORKING_DB_PATH", root / ".registry_working.db"),
            ("DATA_REPO_PATH", root / "data-repo"),
            ("DATA_REPO_URL", str(self.remote)),
        ):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Real git commits need an identity - set it via env rather than
        # relying on whatever this machine's global git config happens to
        # have, so the test is hermetic.
        env_patcher = mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        })
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _seed_local(self):
        conn = db.get_connection()
        add_channel(conn, "UC_a", display_name="Artist A")
        add_video(conn, "v0", "UC_a", song_id=1)
        conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) VALUES (1, 'UC_a', 'Song A', '2026-08-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO rejected_videos (video_id, channel_id, title, checked_at) VALUES (?, ?, ?, ?)",
            ("r0", "UC_a", "Making Film", "2026-08-01T00:00:00"),
        )
        add_snapshot(conn, "v0", "2026-08-01", 100)
        conn.commit()
        conn.close()

    def _table_rows(self, conn, table):
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]

    def test_round_trip_reproduces_every_table(self):
        self._seed_local()
        db.push_from_local()

        before = db.get_connection()
        snapshot_state = {t: self._table_rows(before, t) for t in db._TABLES}
        before.close()

        db.LOCAL_WORKING_DB_PATH.unlink()
        db.pull_to_local()

        after = db.get_connection()
        for table in db._TABLES:
            self.assertEqual(self._table_rows(after, table), snapshot_state[table], table)
        after.close()

    def test_dump_files_are_plain_text_not_a_binary_db(self):
        self._seed_local()
        db.push_from_local()

        videos_sql = (db.DATA_REPO_PATH / "videos.sql").read_text(encoding="utf-8")
        self.assertIn("INSERT INTO videos", videos_sql)
        self.assertIn("'v0'", videos_sql)

        chunk = db.DATA_REPO_PATH / "rejected_videos" / "2026-08.sql"
        self.assertTrue(chunk.exists())
        self.assertIn("'r0'", chunk.read_text(encoding="utf-8"))

    def test_second_push_with_no_changes_does_not_duplicate_rows_or_commit(self):
        self._seed_local()
        db.push_from_local()

        chunk = db.DATA_REPO_PATH / "rejected_videos" / "2026-08.sql"
        content_after_first_push = chunk.read_text(encoding="utf-8")
        log_after_first_push = subprocess.run(
            ["git", "log", "--oneline"], cwd=db.DATA_REPO_PATH, capture_output=True, text=True, check=True
        ).stdout

        db.push_from_local()

        self.assertEqual(chunk.read_text(encoding="utf-8"), content_after_first_push, "no duplicate append")
        log_after_second_push = subprocess.run(
            ["git", "log", "--oneline"], cwd=db.DATA_REPO_PATH, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(log_after_second_push, log_after_first_push, "nothing changed - no new commit")

    def test_append_only_push_adds_new_rows_without_touching_old_ones(self):
        self._seed_local()
        db.push_from_local()

        conn = db.get_connection()
        add_snapshot(conn, "v0", "2026-08-02", 150)
        conn.commit()
        conn.close()

        db.push_from_local()

        chunk = (db.DATA_REPO_PATH / "view_snapshots" / "2026-08.sql").read_text(encoding="utf-8")
        self.assertIn("'2026-08-01'", chunk)
        self.assertIn("'2026-08-02'", chunk)
        self.assertEqual(chunk.count("INSERT INTO view_snapshots"), 2)

    def test_rare_delete_triggers_full_regeneration_not_a_stale_append(self):
        """
        The only thing that ever deletes from view_snapshots/rejected_videos
        is discovery.py's _purge_excluded (channel exclusion) - simulated
        here directly as a row delete, since that's the observable effect
        db.py's push logic needs to detect (see _needs_full_regen).
        """
        self._seed_local()
        db.push_from_local()

        conn = db.get_connection()
        conn.execute("DELETE FROM view_snapshots WHERE video_id = 'v0' AND snapshot_date = '2026-08-01'")
        add_video(conn, "v1", "UC_a", song_id=1)
        add_snapshot(conn, "v1", "2026-08-01", 999)
        conn.commit()
        conn.close()

        db.push_from_local()

        db.LOCAL_WORKING_DB_PATH.unlink()
        db.pull_to_local()
        rebuilt = db.get_connection()
        rows = {(r["video_id"], r["snapshot_date"]) for r in rebuilt.execute("SELECT video_id, snapshot_date FROM view_snapshots")}
        rebuilt.close()
        self.assertNotIn(("v0", "2026-08-01"), rows, "the deleted row must not survive a regeneration")
        self.assertIn(("v1", "2026-08-01"), rows, "the new row must still be present")

    def test_pull_clones_when_no_local_checkout_exists_yet(self):
        self._seed_local()
        db.push_from_local()

        # A different "machine": no local data-repo checkout at all yet.
        _rmtree_git_checkout(db.DATA_REPO_PATH)
        db.LOCAL_WORKING_DB_PATH.unlink()

        db.pull_to_local()

        conn = db.get_connection()
        channel_ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channels")}
        conn.close()
        self.assertEqual(channel_ids, {"UC_a"})

    def test_pull_discards_local_only_rows_not_yet_pushed(self):
        """
        pull_to_local() is always a full rebuild now - a locally-added row
        that was never pushed must not survive a pull, same as the old
        override=True behavior (there is no merge mode anymore; see db.py's
        module docstring).
        """
        self._seed_local()
        db.push_from_local()

        conn = db.get_connection()
        add_channel(conn, "UC_local_only")
        conn.commit()
        conn.close()

        db.pull_to_local()

        conn = db.get_connection()
        channel_ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channels")}
        conn.close()
        self.assertNotIn("UC_local_only", channel_ids)

    def test_pull_to_local_backs_up_before_rebuilding(self):
        self._seed_local()
        db.push_from_local()

        stale_marker = db.LOCAL_WORKING_DB_PATH.read_bytes()
        db.pull_to_local()

        backups = sorted(db.LOCAL_WORKING_DB_PATH.parent.glob("registry.*.pre-pull.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), stale_marker)

    def test_pull_recovers_from_a_corrupted_local_file(self):
        """
        The local file has been genuinely corrupted before in this project
        (data/.registry_working.db.corrupted-backup-before-restore) - a
        pull must still self-heal by discarding and rebuilding it rather
        than raising.
        """
        self._seed_local()
        db.push_from_local()

        db.LOCAL_WORKING_DB_PATH.write_text("not a valid sqlite file")
        db.pull_to_local()

        conn = db.get_connection()
        channel_ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channels")}
        conn.close()
        self.assertEqual(channel_ids, {"UC_a"})

    def test_push_without_data_repo_url_raises(self):
        with mock.patch.object(db, "DATA_REPO_URL", None):
            with self.assertRaises(EnvironmentError):
                db.push_from_local()


if __name__ == "__main__":
    unittest.main()
