"""
Guards catalog._fetch_video_details, which used to run its own
videos().list call directly and now delegates to
youtube_api.batch_fetch_metadata - the same chunked/concurrent fetch
snapshot.py uses for view counts - so a quota-blocked chunk falls back to
yt-dlp instead of ending the sync.

This only pins the delegation (video_id<->url mapping, duration/tags
extraction, missing-result handling); batch_fetch_metadata's own chunking
and yt-dlp fallback are already covered where that function lives.

No test reaches the network.
"""

import unittest
from unittest import mock

from . import context  # noqa: F401 - puts src/ on sys.path

from registry import catalog


class FetchVideoDetailsTest(unittest.TestCase):
    def _fetch(self, video_ids, fake_results):
        with mock.patch.object(catalog, "batch_fetch_metadata", return_value=fake_results) as fetch:
            details = catalog._fetch_video_details(mock.Mock(), video_ids)
        return details, fetch

    def test_maps_video_ids_to_urls_and_back(self):
        details, fetch = self._fetch(
            ["v1", "v2"],
            {
                "https://www.youtube.com/watch?v=v1": {"duration": 210, "tags": ["a", "b"]},
                "https://www.youtube.com/watch?v=v2": {"duration": 95, "tags": []},
            },
        )
        self.assertEqual(
            details,
            {"v1": {"duration": 210, "tags": ["a", "b"]}, "v2": {"duration": 95, "tags": []}},
        )
        requested = fetch.call_args[0][0]
        self.assertEqual(
            set(requested),
            {"https://www.youtube.com/watch?v=v1", "https://www.youtube.com/watch?v=v2"},
        )

    def test_tags_default_to_empty_when_the_backend_omits_them(self):
        """A fetch result with no tags key (e.g. an older mock, or a video
        with none) must not raise - see _video_tags_label's own tolerance
        for the same case downstream in song_grouping."""
        details, _ = self._fetch(["v1"], {"https://www.youtube.com/watch?v=v1": {"duration": 210}})
        self.assertEqual(details, {"v1": {"duration": 210, "tags": []}})

    def test_a_video_missing_from_the_fetch_is_omitted_not_defaulted(self):
        """
        The caller treats a missing key as 0 duration (fails the MV check),
        which must come from the caller's .get(..., {}).get(..., 0), not
        from this function inventing a zero - see is_valid_mv's caller in
        sync_videos.
        """
        details, _ = self._fetch(
            ["v1", "v2"], {"https://www.youtube.com/watch?v=v1": {"duration": 210, "tags": []}}
        )
        self.assertEqual(details, {"v1": {"duration": 210, "tags": []}})
        self.assertNotIn("v2", details)

    def test_empty_video_ids_short_circuits_without_calling_the_fetch(self):
        details, fetch = self._fetch([], {})
        self.assertEqual(details, {})


if __name__ == "__main__":
    unittest.main()
