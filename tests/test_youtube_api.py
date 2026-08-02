"""
Guards the YouTube Data API backend.

Two things matter here beyond correctness. Its output shape is shared with
the yt-dlp backend, since pipeline.py picks one at import time and never
branches again - a field present in one and not the other breaks only the
configuration nobody is currently running. And quota is the scarce
resource, so how many requests a call makes, and how wide they are, is
behaviour worth pinning rather than an implementation detail.

No test reaches the network: the client is always stubbed.
"""

import unittest
from datetime import date
from unittest import mock

from . import context  # noqa: F401  (puts src/ on sys.path)

from shared import api_budget, youtube_api


def _video(video_id, views=1000, published="2024-03-05T00:00:00Z",
           title="Artist - Song (Official MV)", duration="PT3M30S"):
    return {
        "id": video_id,
        "statistics": {"viewCount": str(views)},
        "snippet": {"publishedAt": published, "title": title, "channelTitle": "Artist"},
        "contentDetails": {"duration": duration},
    }


class ParsingTest(unittest.TestCase):
    def test_video_ids_are_extracted_from_both_url_forms(self):
        for url in ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://youtu.be/dQw4w9WgXcQ",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz"]:
            with self.subTest(url=url):
                self.assertEqual(youtube_api.extract_video_id(url), "dQw4w9WgXcQ")

    def test_a_url_with_no_video_id_raises(self):
        with self.assertRaises(ValueError):
            youtube_api.extract_video_id("https://example.com/not-a-video")

    def test_iso_durations(self):
        for text, seconds in [("PT3M30S", 210), ("PT1H2M3S", 3723), ("PT45S", 45),
                              ("PT2H", 7200), ("", 0), ("garbage", 0)]:
            with self.subTest(text=text):
                self.assertEqual(youtube_api.parse_iso_duration(text), seconds)

    def test_requests_are_packed_to_the_api_maximum(self):
        """A narrower request costs the same unit, so it wastes quota."""
        ids = [f"id{i}" for i in range(youtube_api.MAX_IDS_PER_REQUEST * 2 + 1)]
        chunks = youtube_api.chunked_ids(ids)
        self.assertEqual([len(c) for c in chunks],
                         [youtube_api.MAX_IDS_PER_REQUEST, youtube_api.MAX_IDS_PER_REQUEST, 1])
        self.assertEqual([i for c in chunks for i in c], ids)

    def test_a_missing_publish_date_falls_back_rather_than_raising(self):
        """A usable video shouldn't be discarded over a presentational field."""
        meta = youtube_api._meta_from_item(_video("x", published=""))
        self.assertEqual(meta["release_year"], date.today().year)


class BatchFetchMetadataTest(unittest.TestCase):
    def setUp(self):
        self.recorded = []
        p = mock.patch.object(api_budget, "record_youtube_units",
                              side_effect=lambda n: self.recorded.append(n))
        p.start()
        self.addCleanup(p.stop)

    def _client_returning(self, items_by_call):
        calls = []

        def _list(part, id):
            calls.append(id.split(","))
            return mock.Mock(execute=lambda: {"items": items_by_call.pop(0)})

        client = mock.Mock()
        client.videos.return_value.list.side_effect = _list
        return client, calls

    def test_one_request_per_fifty_ids(self):
        urls = [f"https://youtu.be/{i:011d}" for i in range(120)]
        client, calls = self._client_returning([[], [], []])
        with mock.patch.object(youtube_api, "get_client", return_value=client):
            youtube_api.batch_fetch_metadata(urls, max_workers=1)
        self.assertEqual([len(c) for c in calls], [50, 50, 20])
        self.assertEqual(self.recorded, [1, 1, 1], "1 unit per request regardless of width")

    def test_returns_metadata_keyed_by_url(self):
        client, _ = self._client_returning([[_video("dQw4w9WgXcQ", views=5000)]])
        with mock.patch.object(youtube_api, "get_client", return_value=client):
            result = youtube_api.batch_fetch_metadata(
                ["https://youtu.be/dQw4w9WgXcQ"], max_workers=1)
        self.assertEqual(list(result), ["https://youtu.be/dQw4w9WgXcQ"])
        self.assertEqual(result["https://youtu.be/dQw4w9WgXcQ"]["views"], 5000)

    def test_unparseable_urls_are_skipped_without_spending_quota(self):
        with mock.patch.object(youtube_api, "get_client") as client:
            self.assertEqual(youtube_api.batch_fetch_metadata(["not a url"]), {})
            client.assert_not_called()
        self.assertEqual(self.recorded, [])

    def test_a_deleted_video_is_absent_rather_than_an_error(self):
        """Callers treat a missing URL as 'couldn't fetch, skip'."""
        client, _ = self._client_returning([[]])
        with mock.patch.object(youtube_api, "get_client", return_value=client):
            self.assertEqual(
                youtube_api.batch_fetch_metadata(["https://youtu.be/dQw4w9WgXcQ"], max_workers=1),
                {},
            )

    def test_one_chunk_exhausting_budget_does_not_discard_the_others(self):
        """Chunks run concurrently; a late refusal must not undo earlier work."""
        def _record(n):
            self.recorded.append(n)
            if len(self.recorded) == 2:
                raise api_budget.QuotaExceededError("budget spent")

        client, _ = self._client_returning([[_video(f"{i:011d}")] for i in range(3)])
        with mock.patch.object(api_budget, "record_youtube_units", side_effect=_record), \
             mock.patch.object(youtube_api, "get_client", return_value=client), \
             mock.patch("shared.metadata.batch_fetch_metadata", return_value={}) as ytdlp_fallback:
            result = youtube_api.batch_fetch_metadata(
                [f"https://youtu.be/{i:011d}" for i in range(120)], max_workers=1)
        self.assertTrue(result, "results fetched before the limit must survive")
        ytdlp_fallback.assert_called_once()  # the quota-blocked chunks, retried via yt-dlp


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.recorded = []
        p = mock.patch.object(api_budget, "record_youtube_units",
                              side_effect=lambda n: self.recorded.append(n))
        p.start()
        self.addCleanup(p.stop)

    def _client(self, video_items):
        client = mock.Mock()
        client.search.return_value.list.return_value.execute.return_value = {
            "items": [{"id": {"videoId": v["id"]}} for v in video_items]
        }
        client.videos.return_value.list.return_value.execute.return_value = {"items": video_items}
        return client

    def test_a_search_costs_a_hundred_units_and_the_lookup_one(self):
        with mock.patch.object(youtube_api, "get_client",
                               return_value=self._client([_video("aaaaaaaaaaa")])):
            youtube_api.search_kpop("kpop", limit=5)
        self.assertEqual(self.recorded,
                         [youtube_api.UNITS_PER_SEARCH, youtube_api.UNITS_PER_LIST])

    def test_an_empty_search_skips_the_second_call(self):
        with mock.patch.object(youtube_api, "get_client", return_value=self._client([])):
            self.assertEqual(youtube_api.search_kpop("nothing"), [])
        self.assertEqual(self.recorded, [youtube_api.UNITS_PER_SEARCH])

    def test_results_are_sorted_by_views_and_limited(self):
        items = [_video(f"{i:011d}", views=i * 100) for i in range(1, 6)]
        with mock.patch.object(youtube_api, "get_client", return_value=self._client(items)):
            songs = youtube_api.search_kpop("kpop", limit=3, filter_mv=False)
        self.assertEqual([s["views"] for s in songs], [500, 400, 300])

    def test_non_mv_results_are_filtered_when_asked(self):
        items = [_video("aaaaaaaaaaa", title="Artist - Song (Official MV)"),
                 _video("bbbbbbbbbbb", title="Artist Vlog Episode 3", duration="PT3M")]
        with mock.patch.object(youtube_api, "get_client", return_value=self._client(items)):
            songs = youtube_api.search_kpop("kpop", filter_mv=True)
        self.assertEqual([s["id"] for s in songs], ["aaaaaaaaaaa"])

    def test_result_shape_matches_the_yt_dlp_backend(self):
        """pipeline.py picks a backend at import and never branches again."""
        from shared import search as ytdlp_search  # noqa: F401  (import parity check)

        with mock.patch.object(youtube_api, "get_client",
                               return_value=self._client([_video("aaaaaaaaaaa")])):
            song = youtube_api.search_kpop("kpop", filter_mv=False)[0]
        for field in ["id", "title", "uploader", "views", "upload_date", "duration",
                      "release_year", "release_date", "years_on_chart", "months_on_chart", "url"]:
            self.assertIn(field, song)


if __name__ == "__main__":
    unittest.main()
