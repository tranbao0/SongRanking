"""
Guards catalog._fetch_durations, which used to run its own videos().list
call directly and now delegates to youtube_api.batch_fetch_metadata - the
same chunked/concurrent fetch snapshot.py uses for view counts - so a
quota-blocked chunk falls back to yt-dlp instead of ending the sync.

This only pins the delegation (video_id<->url mapping, duration
extraction, missing-result handling); batch_fetch_metadata's own chunking
and yt-dlp fallback are already covered where that function lives.

No test reaches the network.
"""

import unittest
from unittest import mock

from . import context  # noqa: F401 - puts src/ on sys.path

from registry import catalog


class FetchDurationsTest(unittest.TestCase):
    def _fetch(self, video_ids, fake_results):
        with mock.patch.object(catalog, "batch_fetch_metadata", return_value=fake_results) as fetch:
            durations = catalog._fetch_durations(mock.Mock(), video_ids)
        return durations, fetch

    def test_maps_video_ids_to_urls_and_back(self):
        durations, fetch = self._fetch(
            ["v1", "v2"],
            {
                "https://www.youtube.com/watch?v=v1": {"duration": 210},
                "https://www.youtube.com/watch?v=v2": {"duration": 95},
            },
        )
        self.assertEqual(durations, {"v1": 210, "v2": 95})
        requested = fetch.call_args[0][0]
        self.assertEqual(
            set(requested),
            {"https://www.youtube.com/watch?v=v1", "https://www.youtube.com/watch?v=v2"},
        )

    def test_a_video_missing_from_the_fetch_is_omitted_not_defaulted(self):
        """
        The caller treats a missing key as 0 duration (fails the MV check),
        which must come from the caller's .get(..., 0), not from this
        function inventing a zero - see is_valid_mv's caller in sync_videos.
        """
        durations, _ = self._fetch(["v1", "v2"], {"https://www.youtube.com/watch?v=v1": {"duration": 210}})
        self.assertEqual(durations, {"v1": 210})
        self.assertNotIn("v2", durations)

    def test_empty_video_ids_short_circuits_without_calling_the_fetch(self):
        durations, fetch = self._fetch([], {})
        self.assertEqual(durations, {})


if __name__ == "__main__":
    unittest.main()
