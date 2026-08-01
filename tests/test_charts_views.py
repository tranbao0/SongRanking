"""
Guards charts._latest_and_baseline_views, which was rewritten from "read
every snapshot for the genre and keep the last one per video in Python"
into two indexed lookups per video.

The main test here is differential: the previous implementation is kept
verbatim below as a reference oracle, and both are run over randomised
snapshot histories. That is a stronger check than asserting fixed expected
values, because the interesting cases are the awkward ones - videos with no
snapshots, gaps in history, a baseline landing exactly on the cutoff, a
window older than any snapshot taken.
"""

import random
import unittest
from datetime import date, timedelta

from .context import make_db, add_channel, add_video, add_snapshot

import charts


def _reference_impl(conn, genre, window_days):
    """The pre-rewrite implementation, kept as an oracle. Do not optimise."""
    rows = conn.execute(
        """
        SELECT vs.video_id, vs.snapshot_date, vs.views
        FROM view_snapshots vs
        JOIN videos v ON v.video_id = vs.video_id
        JOIN channels c ON c.channel_id = v.channel_id
        WHERE c.genre = ?
        ORDER BY vs.video_id, vs.snapshot_date
        """,
        (genre,),
    ).fetchall()

    cutoff = (date.today() - timedelta(days=window_days)).isoformat() if window_days else None

    result = {}
    for row in rows:
        entry = result.setdefault(row["video_id"], {"latest": None, "baseline": None})
        entry["latest"] = row["views"]
        if cutoff is not None and row["snapshot_date"] <= cutoff:
            entry["baseline"] = row["views"]
    return result


class LatestAndBaselineViewsTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_matches_reference_on_randomised_histories(self):
        rng = random.Random(20260801)
        add_channel(self.conn, "UC_kpop", genre="kpop")
        add_channel(self.conn, "UC_jpop", genre="jpop")

        today = date.today()
        for i in range(40):
            channel = "UC_kpop" if i % 4 else "UC_jpop"
            video_id = f"vid{i:03d}"
            add_video(self.conn, video_id, channel)

            # Deliberately includes videos with zero snapshots, sparse
            # histories with gaps, and histories that stop well in the past.
            for day in rng.sample(range(0, 60), rng.choice([0, 0, 1, 3, 17, 40])):
                add_snapshot(
                    self.conn, video_id,
                    (today - timedelta(days=day)).isoformat(),
                    rng.randint(0, 5_000_000),
                )
        self.conn.commit()

        for genre in ("kpop", "jpop"):
            for window_days in (None, 0, 1, 7, 30, 365):
                with self.subTest(genre=genre, window_days=window_days):
                    self.assertEqual(
                        charts._latest_and_baseline_views(self.conn, genre, window_days),
                        _reference_impl(self.conn, genre, window_days),
                    )

    def test_video_without_snapshots_is_omitted(self):
        """A never-measured video must not look like a real zero."""
        add_channel(self.conn, "UC_kpop")
        add_video(self.conn, "measured", "UC_kpop")
        add_video(self.conn, "unmeasured", "UC_kpop")
        add_snapshot(self.conn, "measured", date.today().isoformat(), 100)
        self.conn.commit()

        result = charts._latest_and_baseline_views(self.conn, "kpop", None)
        self.assertIn("measured", result)
        self.assertNotIn("unmeasured", result)

    def test_latest_is_the_most_recent_snapshot(self):
        add_channel(self.conn, "UC_kpop")
        add_video(self.conn, "v1", "UC_kpop")
        # Inserted out of order: correctness must come from snapshot_date,
        # not from insertion or rowid order.
        add_snapshot(self.conn, "v1", "2026-03-01", 300)
        add_snapshot(self.conn, "v1", "2026-01-01", 100)
        add_snapshot(self.conn, "v1", "2026-02-01", 200)
        self.conn.commit()

        self.assertEqual(charts._latest_and_baseline_views(self.conn, "kpop", None)["v1"]["latest"], 300)

    def test_baseline_is_nearest_snapshot_not_after_cutoff(self):
        add_channel(self.conn, "UC_kpop")
        add_video(self.conn, "v1", "UC_kpop")
        today = date.today()
        for days_ago, views in ((0, 500), (5, 400), (7, 300), (9, 200), (40, 100)):
            add_snapshot(self.conn, "v1", (today - timedelta(days=days_ago)).isoformat(), views)
        self.conn.commit()

        entry = charts._latest_and_baseline_views(self.conn, "kpop", 7)["v1"]
        self.assertEqual(entry["latest"], 500)
        # Exactly on the cutoff counts as "not after" it.
        self.assertEqual(entry["baseline"], 300)

    def test_baseline_is_none_when_history_is_too_short(self):
        """Too little history must read as unknown, not as a gain of zero."""
        add_channel(self.conn, "UC_kpop")
        add_video(self.conn, "v1", "UC_kpop")
        add_snapshot(self.conn, "v1", date.today().isoformat(), 500)
        self.conn.commit()

        self.assertIsNone(charts._latest_and_baseline_views(self.conn, "kpop", 30)["v1"]["baseline"])

    def test_baseline_not_computed_without_a_window(self):
        add_channel(self.conn, "UC_kpop")
        add_video(self.conn, "v1", "UC_kpop")
        add_snapshot(self.conn, "v1", "2020-01-01", 100)
        add_snapshot(self.conn, "v1", date.today().isoformat(), 500)
        self.conn.commit()

        self.assertIsNone(charts._latest_and_baseline_views(self.conn, "kpop", None)["v1"]["baseline"])

    def test_other_genres_are_excluded(self):
        add_channel(self.conn, "UC_kpop", genre="kpop")
        add_channel(self.conn, "UC_jpop", genre="jpop")
        add_video(self.conn, "k1", "UC_kpop")
        add_video(self.conn, "j1", "UC_jpop")
        add_snapshot(self.conn, "k1", date.today().isoformat(), 1)
        add_snapshot(self.conn, "j1", date.today().isoformat(), 2)
        self.conn.commit()

        self.assertEqual(list(charts._latest_and_baseline_views(self.conn, "kpop", None)), ["k1"])

    def test_uses_an_index_rather_than_scanning_snapshots(self):
        """
        The point of the rewrite: cost must not grow with history length.
        A full SCAN of view_snapshots would reintroduce exactly that.
        """
        add_channel(self.conn, "UC_kpop")
        add_video(self.conn, "v1", "UC_kpop")
        self.conn.commit()

        plan = " ".join(
            row["detail"]
            for row in self.conn.execute(
                "EXPLAIN QUERY PLAN " + charts._LATEST_AND_BASELINE_VIEWS_SQL, ("2026-01-01", "kpop")
            )
        )
        self.assertNotIn("SCAN view_snapshots", plan)
        self.assertNotIn("SCAN s", plan)


if __name__ == "__main__":
    unittest.main()
