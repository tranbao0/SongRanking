"""
Guards the registry schema and snapshot.take_snapshot's pending query.

The index assertions are deliberately about query *plans* rather than about
which indexes exist: the point of the change was that snapshot lookups stop
scanning a table that grows by one row per video per day, and a plan is the
only thing that actually states that.
"""

import sqlite3
import unittest
from datetime import date
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
            add_video(self.conn, f"v{i}", "UC_a")
        self.conn.commit()

        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_with_views(self, views_by_url):
        with mock.patch.object(snapshot, "batch_fetch_metadata",
                               return_value={u: {"views": v} for u, v in views_by_url.items()}) as fetch:
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


if __name__ == "__main__":
    unittest.main()
