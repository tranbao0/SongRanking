"""
Guards the registry schema and snapshot.take_snapshot's pending query.

The index assertions are deliberately about query *plans* rather than about
which indexes exist: the point of the change was that snapshot lookups stop
scanning a table that grows by one row per video per day, and a plan is the
only thing that actually states that.
"""

import sqlite3
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
        self.assertIn("song_id", {r["name"] for r in self.conn.execute("PRAGMA table_info(videos)")})

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
    Guards pull_to_local()'s pre-overwrite backup: a pull replaces the local
    working copy wholesale with Turso's current state, so without a backup,
    whatever not-yet-pushed local work it clobbers would be unrecoverable.
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

    def test_pull_to_local_backs_up_before_overwriting(self):
        db.LOCAL_WORKING_DB_PATH.write_text("stale local work")
        remote = make_db(keep_open=True)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_a")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.pull_to_local()

        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "stale local work")


class MergeModeTest(unittest.TestCase):
    """
    Guards override=False on pull_to_local()/push_from_local() - the mode
    `python run.py pull`/`push` default to (see run.py's --override flag).
    Merge is an upsert by primary key: a row the destination is missing
    gets added, a row it already has gets its columns overwritten with
    the source's current values (so changes actually propagate, not just
    new rows), and a row that only exists on the destination is never
    deleted. Unlike override=True, pull-merge must also never touch the
    local file wholesale.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = mock.patch.object(db, "LOCAL_WORKING_DB_PATH", Path(self.tmpdir.name) / ".registry_working.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _local_channel_names(self):
        conn = db.get_connection()
        try:
            return {r["channel_id"]: r["display_name"] for r in conn.execute("SELECT channel_id, display_name FROM channels")}
        finally:
            conn.close()

    def test_pull_merge_applies_remote_changes_and_adds_missing_rows(self):
        local = db.get_connection()
        add_channel(local, "UC_a", display_name="Local Stale Name")
        local.commit()
        local.close()

        remote = make_db(keep_open=True)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_a", display_name="Remote Updated Name")
        add_channel(remote, "UC_b", display_name="Remote Only")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.pull_to_local(override=False)

        names = self._local_channel_names()
        self.assertEqual(names["UC_a"], "Remote Updated Name", "a merge pull must carry remote's changes to a row local already has")
        self.assertEqual(names["UC_b"], "Remote Only", "a remote-only row must be added by a merge pull")

    def test_pull_merge_never_wipes_or_backs_up_the_local_file(self):
        local = db.get_connection()
        add_channel(local, "UC_a")
        local.commit()
        local.close()

        remote = make_db(keep_open=True)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_b")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.pull_to_local(override=False)

        backups = sorted(db.LOCAL_WORKING_DB_PATH.parent.glob("registry.*.pre-pull.db"))
        self.assertEqual(backups, [], "a merge pull is non-destructive, so it shouldn't need a backup")

    def test_push_merge_applies_local_changes_and_adds_missing_rows(self):
        local = db.get_connection()
        add_channel(local, "UC_a", display_name="Local Updated Name")
        add_channel(local, "UC_b", display_name="Local Only")
        local.commit()
        local.close()

        remote = make_db(keep_open=True, check_same_thread=False)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_a", display_name="Remote Stale Name")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.push_from_local(override=False)

        rows = {r["channel_id"]: r["display_name"] for r in remote.execute("SELECT channel_id, display_name FROM channels")}
        self.assertEqual(rows["UC_a"], "Local Updated Name", "a merge push must carry local's changes to a row remote already has")
        self.assertEqual(rows["UC_b"], "Local Only", "a local-only row must be added by a merge push")

    def test_push_merge_does_not_delete_remote_only_rows(self):
        local = db.get_connection()
        add_channel(local, "UC_a")
        local.commit()
        local.close()

        remote = make_db(keep_open=True, check_same_thread=False)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_a")
        add_channel(remote, "UC_remote_only")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.push_from_local(override=False)

        channel_ids = {r["channel_id"] for r in remote.execute("SELECT channel_id FROM channels")}
        self.assertIn("UC_remote_only", channel_ids, "merge must never delete a row that only exists on the destination")

    def test_push_override_still_replaces_the_hosted_registry_wholesale(self):
        local = db.get_connection()
        add_channel(local, "UC_new")
        local.commit()
        local.close()

        remote = make_db(keep_open=True, check_same_thread=False)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_old")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.push_from_local(override=True)

        channel_ids = {r["channel_id"] for r in remote.execute("SELECT channel_id FROM channels")}
        self.assertEqual(channel_ids, {"UC_new"})

    def test_pull_override_applies_remote_changes_and_deletes_local_only_rows(self):
        local = db.get_connection()
        add_channel(local, "UC_a", display_name="Local Stale Name")
        add_channel(local, "UC_local_only")
        local.commit()
        local.close()

        remote = make_db(keep_open=True)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_a", display_name="Remote Updated Name")
        add_channel(remote, "UC_b", display_name="Remote Only")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.pull_to_local(override=True)

        names = self._local_channel_names()
        self.assertEqual(names["UC_a"], "Remote Updated Name")
        self.assertEqual(names["UC_b"], "Remote Only")
        self.assertNotIn("UC_local_only", names,
                          "override pull must discard not-yet-pushed local-only rows, same as the old wipe-and-rebuild")

    def test_pull_recovers_from_a_corrupted_local_file(self):
        """
        The local file has been genuinely corrupted before in this project
        (data/.registry_working.db.corrupted-backup-before-restore) - a
        pull must still self-heal by discarding and rebuilding it rather
        than raising, since a corrupted file has nothing an upsert could
        preserve anyway.
        """
        db.LOCAL_WORKING_DB_PATH.write_text("not a valid sqlite file")

        remote = make_db(keep_open=True)
        self.addCleanup(remote.really_close)
        add_channel(remote, "UC_a")
        remote.commit()

        with mock.patch.object(db, "get_remote_connection", return_value=remote):
            db.pull_to_local(override=True)

        names = self._local_channel_names()
        self.assertEqual(set(names), {"UC_a"})


if __name__ == "__main__":
    unittest.main()
