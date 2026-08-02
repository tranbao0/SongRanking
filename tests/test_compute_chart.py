"""
End-to-end cover for charts.compute_chart, so the rewritten view lookup is
exercised through the path that actually renders rather than only in
isolation - including song grouping, which is the reason a chart entry's
view count is a sum rather than a single video's number.

Chart definitions are stubbed rather than read from data/charts.yaml, so
these tests don't break when a real chart's limit or window is retuned.
"""

import unittest
from datetime import date, timedelta
from unittest import mock

from .context import make_db, add_channel, add_video, add_snapshot

import charts
from registry import db


class ComputeChartTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        self.today = date.today()

        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

        add_channel(self.conn, "UC_a", genre="kpop", display_name="Artist A")

    def _define(self, **overrides):
        definition = {"name": "t", "genre": "kpop", "metric": "cumulative",
                      "window_days": None, "limit": 10}
        definition.update(overrides)
        return mock.patch.object(charts, "_load_definition", return_value=definition)

    def _snapshot(self, video_id, days_ago, views):
        add_snapshot(self.conn, video_id, (self.today - timedelta(days=days_ago)).isoformat(), views)

    def test_cumulative_sums_views_across_a_songs_uploads(self):
        """
        The whole point of grouping: an MV plus its dance practice is one
        chart entry whose total is both uploads combined.
        """
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) "
            "VALUES (1, 'UC_a', 'Grouped Song', '2026-01-01')"
        )
        add_video(self.conn, "mv", "UC_a", title="Grouped Song (Official MV)", song_id=1)
        add_video(self.conn, "practice", "UC_a", title="Grouped Song (Dance Practice)", song_id=1)
        add_video(self.conn, "solo", "UC_a", title="Ungrouped Song", song_id=2)
        self._snapshot("mv", 0, 700)
        self._snapshot("practice", 0, 300)
        self._snapshot("solo", 0, 900)
        self.conn.commit()

        with self._define():
            results = charts.compute_chart("t")

        self.assertEqual([(r["title"], r["views"]) for r in results],
                         [("Grouped Song", 1000), ("Ungrouped Song", 900)])

    def test_representative_is_the_highest_viewed_member(self):
        """It supplies the url that actually gets downloaded."""
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) "
            "VALUES (1, 'UC_a', 'Song', '2026-01-01')"
        )
        add_video(self.conn, "low", "UC_a", song_id=1)
        add_video(self.conn, "high", "UC_a", song_id=1)
        self._snapshot("low", 0, 10)
        self._snapshot("high", 0, 999)
        self.conn.commit()

        with self._define():
            self.assertEqual(charts.compute_chart("t")[0]["url"],
                             "https://www.youtube.com/watch?v=high")

    def test_gained_ranks_by_delta_and_skips_videos_without_a_baseline(self):
        add_video(self.conn, "steady", "UC_a", title="Steady", song_id=1)
        add_video(self.conn, "surging", "UC_a", title="Surging", song_id=2)
        add_video(self.conn, "brand_new", "UC_a", title="Brand New", song_id=3)
        self._snapshot("steady", 10, 1000)
        self._snapshot("steady", 0, 1100)
        self._snapshot("surging", 10, 500)
        self._snapshot("surging", 0, 5000)
        self._snapshot("brand_new", 0, 400)  # no history old enough to compare
        self.conn.commit()

        with self._define(metric="gained", window_days=7):
            results = charts.compute_chart("t")

        self.assertEqual([r["title"] for r in results], ["Surging", "Steady"])

    def test_newest_ranks_by_publish_date(self):
        add_video(self.conn, "old", "UC_a", title="Old", published_at="2020-01-01T00:00:00Z", song_id=1)
        add_video(self.conn, "new", "UC_a", title="New", published_at="2026-01-01T00:00:00Z", song_id=2)
        self._snapshot("old", 0, 100)
        self._snapshot("new", 0, 100)
        self.conn.commit()

        with self._define(metric="newest"):
            self.assertEqual([r["title"] for r in charts.compute_chart("t")], ["New", "Old"])

    def test_limit_is_applied(self):
        for i in range(5):
            add_video(self.conn, f"v{i}", "UC_a", title=f"Song {i}", song_id=i + 1)
            self._snapshot(f"v{i}", 0, i * 100)
        self.conn.commit()

        with self._define(limit=2):
            self.assertEqual(len(charts.compute_chart("t")), 2)

    def test_videos_without_snapshots_do_not_chart(self):
        add_video(self.conn, "unmeasured", "UC_a", title="Unmeasured", song_id=1)
        self.conn.commit()

        with self._define():
            self.assertEqual(charts.compute_chart("t"), [])

    def test_ungrouped_videos_never_chart(self):
        """
        song_id NULL means a blocked non-song upload (teaser, "making of",
        etc.) or one mid-regroup after a decouple - never treated as its
        own singleton chart entry.
        """
        add_video(self.conn, "teaser", "UC_a", title="Teaser")
        self._snapshot("teaser", 0, 999999)
        self.conn.commit()

        with self._define():
            self.assertEqual(charts.compute_chart("t"), [])

    def test_empty_registry_returns_no_results(self):
        """run_pipeline treats this as 'run sync first' rather than crashing."""
        self.conn.commit()
        with self._define():
            self.assertEqual(charts.compute_chart("t"), [])

    def test_unknown_metric_is_rejected(self):
        add_video(self.conn, "v1", "UC_a", song_id=1)
        self._snapshot("v1", 0, 1)
        self.conn.commit()

        with self._define(metric="nonsense"), self.assertRaises(ValueError):
            charts.compute_chart("t")

    def test_result_shape_matches_what_songs_from_search_consumes(self):
        add_video(self.conn, "v1", "UC_a", title="Song", published_at="2024-05-13T00:00:00Z", song_id=1)
        self._snapshot("v1", 0, 1234)
        self.conn.commit()

        with self._define():
            entry = charts.compute_chart("t")[0]

        self.assertEqual(
            set(entry),
            {"id", "title", "uploader", "views", "upload_date", "duration",
             "release_year", "release_date", "years_on_chart", "months_on_chart", "url"},
        )
        self.assertEqual(entry["release_date"], "2024.05.13")
        self.assertEqual(entry["uploader"], "Artist A")


if __name__ == "__main__":
    unittest.main()
