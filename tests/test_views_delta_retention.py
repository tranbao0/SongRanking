"""
Views-delta retention: charts.yaml views_delta_days keeps a fixed CSV
views_baseline so overlay "views gained" accumulates across runs until
the window expires, then resets.
"""

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from . import context  # noqa: F401  puts src/ on sys.path

from render.chart_state import (
    compute_views_gained,
    load_history,
    save_run_state,
)


def _song(url, views, last_views="", views_baseline="", views_baseline_at="",
          rank="1", title="Song", artist="Artist"):
    return {
        "url": url,
        "title": title,
        "artist": artist,
        "rank": rank,
        "peak": rank,
        "start": "00:01:00",
        "end": "00:01:15",
        "last_views": last_views,
        "last_rank": "",
        "views_baseline": views_baseline,
        "views_baseline_at": views_baseline_at,
        "_meta": {
            "views": views,
            "release_date": "2024.01.01",
            "years_on_chart": 2,
            "months_on_chart": 18,
            "release_year": 2024,
        },
    }


class TestComputeViewsGained(unittest.TestCase):
    def test_every_run_mode_uses_last_views(self):
        song = _song("u", 2_200_000_000, last_views="2000000000")
        self.assertEqual(compute_views_gained(2_200_000_000, song), 200_000_000)

    def test_active_baseline_accumulates_across_last_views(self):
        today = date(2026, 8, 11)
        song = _song(
            "u", 2_400_000_000,
            last_views="2200000000",
            views_baseline="2000000000",
            views_baseline_at="2026-08-01",
        )
        gained = compute_views_gained(
            2_400_000_000, song, views_delta_days=30, today=today,
        )
        self.assertEqual(gained, 400_000_000)

    def test_expired_baseline_falls_back_to_last_views(self):
        today = date(2026, 9, 1)
        song = _song(
            "u", 2_410_000_000,
            last_views="2400000000",
            views_baseline="2000000000",
            views_baseline_at="2026-08-01",
        )
        gained = compute_views_gained(
            2_410_000_000, song, views_delta_days=30, today=today,
        )
        self.assertEqual(gained, 10_000_000)

    def test_no_history_returns_none(self):
        song = _song("u", 100)
        self.assertIsNone(compute_views_gained(100, song, views_delta_days=30))


class TestSaveRunStateRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmp.name) / "chart.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self):
        with open(self.csv_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_seeds_baseline_from_prior_last_views_on_first_retention_run(self):
        today = date(2026, 8, 1)
        songs = [_song(
            "https://www.youtube.com/watch?v=apt",
            2_200_000_000,
            last_views="2000000000",
            rank="1",
            title="APT.",
            artist="ROSE",
        )]
        save_run_state(songs, self.csv_path, views_delta_days=30, today=today)
        row = self._read()[0]
        self.assertEqual(row["last_views"], "2200000000")
        self.assertEqual(row["views_baseline"], "2000000000")
        self.assertEqual(row["views_baseline_at"], "2026-08-01")

    def test_retains_baseline_within_window(self):
        day0 = date(2026, 8, 1)
        songs = [_song(
            "https://www.youtube.com/watch?v=apt",
            2_200_000_000,
            last_views="2000000000",
        )]
        save_run_state(songs, self.csv_path, views_delta_days=30, today=day0)

        day3 = day0 + timedelta(days=3)
        history = load_history(self.csv_path)
        hist = history["https://www.youtube.com/watch?v=apt"]
        songs = [_song(
            "https://www.youtube.com/watch?v=apt",
            2_400_000_000,
            last_views=hist["last_views"],
            views_baseline=hist["views_baseline"],
            views_baseline_at=hist["views_baseline_at"],
        )]
        save_run_state(songs, self.csv_path, views_delta_days=30, today=day3)
        row = self._read()[0]
        self.assertEqual(row["last_views"], "2400000000")
        self.assertEqual(row["views_baseline"], "2000000000")
        self.assertEqual(row["views_baseline_at"], "2026-08-01")

        gained = compute_views_gained(
            2_400_000_000, songs[0], views_delta_days=30, today=day3,
        )
        self.assertEqual(gained, 400_000_000)

    def test_resets_baseline_after_retention_expires(self):
        day0 = date(2026, 8, 1)
        songs = [_song(
            "https://www.youtube.com/watch?v=apt",
            2_200_000_000,
            last_views="2000000000",
        )]
        save_run_state(songs, self.csv_path, views_delta_days=30, today=day0)

        day30 = day0 + timedelta(days=30)
        history = load_history(self.csv_path)
        hist = history["https://www.youtube.com/watch?v=apt"]
        songs = [_song(
            "https://www.youtube.com/watch?v=apt",
            2_410_000_000,
            last_views=hist["last_views"],
            views_baseline=hist["views_baseline"],
            views_baseline_at=hist["views_baseline_at"],
        )]
        save_run_state(songs, self.csv_path, views_delta_days=30, today=day30)
        row = self._read()[0]
        self.assertEqual(row["last_views"], "2410000000")
        self.assertEqual(row["views_baseline"], "2410000000")
        self.assertEqual(row["views_baseline_at"], "2026-08-31")

    def test_null_retention_does_not_require_baseline(self):
        songs = [_song(
            "https://www.youtube.com/watch?v=x",
            1000,
            last_views="900",
        )]
        save_run_state(songs, self.csv_path, views_delta_days=None, today=date(2026, 8, 1))
        row = self._read()[0]
        self.assertEqual(row["last_views"], "1000")
        self.assertEqual(row.get("views_baseline", ""), "")


class TestKpopAlltimeConfig(unittest.TestCase):
    def test_kpop_alltime_is_monthly(self):
        from charts import load_definition
        definition = load_definition("kpop_alltime")
        self.assertEqual(definition.get("views_delta_days"), 30)


if __name__ == "__main__":
    unittest.main()
