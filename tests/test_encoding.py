"""
Guards encoding's ffprobe memo.

The hazard the cache has to avoid is specific: download_song probes to
decide whether an attempt produced a usable file, and its retry rewrites
that same path. Caching that probe would make a retry read back the failed
attempt's answer. So the memo must only ever hold durations for files that
are final.

No test here spawns ffprobe - _probe_duration is always patched.
"""

import unittest
from unittest import mock

from . import context  # noqa: F401  (puts src/ on sys.path)

from render import encoding


class CachedDurationTest(unittest.TestCase):
    def setUp(self):
        encoding._duration_cache.clear()
        self.addCleanup(encoding._duration_cache.clear)

    def test_probes_once_and_reuses_the_result(self):
        with mock.patch.object(encoding, "_probe_duration", return_value=12.5) as probe:
            self.assertEqual(encoding._cached_duration("clip.mp4"), 12.5)
            self.assertEqual(encoding._cached_duration("clip.mp4"), 12.5)
            self.assertEqual(encoding._cached_duration("clip.mp4"), 12.5)
        self.assertEqual(probe.call_count, 1)

    def test_distinct_paths_are_cached_separately(self):
        with mock.patch.object(encoding, "_probe_duration", side_effect=[1.0, 2.0]):
            self.assertEqual(encoding._cached_duration("a.mp4"), 1.0)
            self.assertEqual(encoding._cached_duration("b.mp4"), 2.0)
        self.assertEqual(encoding._cached_duration("a.mp4"), 1.0)

    def test_failed_probe_is_not_cached(self):
        """
        An unreadable file must stay retryable - caching None would turn a
        transient probe failure into a permanent one for that path.
        """
        with mock.patch.object(encoding, "_probe_duration", side_effect=[None, 9.0]) as probe:
            self.assertIsNone(encoding._cached_duration("clip.mp4"))
            self.assertEqual(encoding._cached_duration("clip.mp4"), 9.0)
        self.assertEqual(probe.call_count, 2)


class DownloadProbeInteractionTest(unittest.TestCase):
    """download_song probes uncached, and seeds the memo only on success."""

    def setUp(self):
        encoding._duration_cache.clear()
        self.addCleanup(encoding._duration_cache.clear)

    @staticmethod
    def _completed(returncode):
        """
        A fresh result per call, deliberately: _attempt mutates returncode
        in place on a failed postcheck, so a shared mock would leak attempt
        one's failure into the retry.
        """
        def _make(*args, **kwargs):
            result = mock.Mock()
            result.returncode = returncode
            result.stderr = ""
            return result
        return _make

    def test_successful_download_seeds_the_cache(self):
        with mock.patch.object(encoding.subprocess, "run", side_effect=self._completed(0)), \
             mock.patch.object(encoding, "_probe_duration", return_value=15.0) as probe:
            raw = encoding.download_song(1, "https://example.com/watch?v=aaaaaaaaaaa")

        self.assertEqual(raw, encoding.cached_clip_path("https://example.com/watch?v=aaaaaaaaaaa"))
        self.assertEqual(encoding._duration_cache[raw], 15.0)
        self.assertEqual(probe.call_count, 1)
        # Downstream consumers now get it for free.
        with mock.patch.object(encoding, "_probe_duration", side_effect=AssertionError("re-probed")):
            self.assertEqual(encoding._cached_duration(raw), 15.0)

    def test_retry_after_unreadable_download_is_not_poisoned_by_the_first_attempt(self):
        """
        The regression this cache could most easily introduce: attempt 1
        yields an unreadable file, the fallback client re-downloads the same
        path successfully, and the final duration must be the retry's.
        """
        with mock.patch.object(encoding.subprocess, "run", side_effect=self._completed(0)), \
             mock.patch.object(encoding, "_probe_duration", side_effect=[None, 15.0]) as probe:
            raw = encoding.download_song(2, "https://example.com/watch?v=bbbbbbbbbbb")

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(encoding._duration_cache[raw], 15.0)

    def test_download_failing_both_attempts_caches_nothing(self):
        with mock.patch.object(encoding.subprocess, "run", side_effect=self._completed(1)), \
             mock.patch.object(encoding, "_probe_duration", return_value=None):
            with self.assertRaises(RuntimeError):
                encoding.download_song(3, "https://example.com/watch?v=ccccccccccc")

        self.assertEqual(encoding._duration_cache, {})


class RawClipCacheTest(unittest.TestCase):
    """
    Guards the raw-clip cache keying: by video ID (stable across runs),
    not by rank/title (which shift as the chart re-ranks) - see
    RAW_CACHE_DIR and pipeline.py's _download.
    """

    def test_cache_path_is_keyed_by_video_id_not_rank_or_title(self):
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        self.assertEqual(
            encoding.cached_clip_path(url),
            f"{encoding.RAW_CACHE_DIR}/aaaaaaaaaaa.mp4",
        )

    def test_same_video_different_urls_share_one_cache_path(self):
        watch_url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        short_url = "https://youtu.be/aaaaaaaaaaa"
        self.assertEqual(encoding.cached_clip_path(watch_url), encoding.cached_clip_path(short_url))

    def test_cached_clip_is_none_when_nothing_is_downloaded_yet(self):
        with mock.patch.object(encoding.os.path, "exists", return_value=False):
            self.assertIsNone(encoding.cached_clip("https://www.youtube.com/watch?v=aaaaaaaaaaa"))

    def test_cached_clip_returns_the_path_once_downloaded(self):
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        with mock.patch.object(encoding.os.path, "exists", return_value=True):
            self.assertEqual(encoding.cached_clip(url), encoding.cached_clip_path(url))


class SafeFilenameTest(unittest.TestCase):
    def test_slugifies_and_truncates(self):
        self.assertEqual(encoding.safe_filename("Hello, World!"), "hello_world")
        self.assertLessEqual(len(encoding.safe_filename("x" * 200)), 50)

    def test_non_ascii_titles_still_produce_a_usable_slug(self):
        self.assertNotIn("/", encoding.safe_filename("설탕 허니 아이스티"))


if __name__ == "__main__":
    unittest.main()
