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
        with mock.patch.object(encoding.os.path, "exists", return_value=True), \
             mock.patch.object(encoding, "_cached_duration", return_value=15.0):
            self.assertEqual(encoding.cached_clip(url), encoding.cached_clip_path(url))

    def test_cached_clip_is_none_when_the_file_is_corrupt(self):
        """A stale/corrupt cache entry (e.g. an interrupted prior run) must
        not be trusted just because it exists - see cached_clip's docstring."""
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        with mock.patch.object(encoding.os.path, "exists", return_value=True), \
             mock.patch.object(encoding, "_cached_duration", return_value=None):
            self.assertIsNone(encoding.cached_clip(url))


class EncodeSongPhaseTest(unittest.TestCase):
    """
    Guards encode_song's phase-level concurrency (see _PHASE_WORKERS): the
    lead/in/static/out/trail phases run in a small thread pool since none
    depends on another's output, but concatenate_clips still has to
    receive them in fixed playback order regardless of which phase
    finishes first, and a failing phase must still surface (not be
    silently swallowed) with every phase file cleaned up.
    """

    def setUp(self):
        self.style = {
            "canvas": {"width": 1920, "height": 1080, "fps": 30},
            "transition": {
                "overlay_type": "wiperight", "overlay_exit_type": "wipeleft",
                "overlay_duration": 0.5, "duration": 1.0,
            },
        }
        p1 = mock.patch.object(encoding, "build_overlay_image", return_value=mock.Mock(save=mock.Mock()))
        p1.start(); self.addCleanup(p1.stop)
        p2 = mock.patch.object(encoding, "_cached_duration", return_value=20.0)
        p2.start(); self.addCleanup(p2.stop)
        p3 = mock.patch.object(encoding.os.path, "exists", return_value=True)
        p3.start(); self.addCleanup(p3.stop)
        p4 = mock.patch.object(encoding.os, "remove")
        self.remove_mock = p4.start()
        self.addCleanup(p4.stop)

    def _encode(self, **overrides):
        kwargs = dict(
            style=self.style, raw_clip="raw.mp4", rank=1, title="Song", artist="Artist",
            peak=1, entry_type="", views=100, release_date="2026.01.01",
            months_on_chart=1, head_trim=0.0, tail_trim=0.0, clips_dir="clips",
        )
        kwargs.update(overrides)
        return encoding.encode_song(**kwargs)

    def test_phases_are_spliced_in_fixed_order_regardless_of_completion_order(self):
        with mock.patch.object(encoding, "_run_ffmpeg") as run_ffmpeg, \
             mock.patch.object(encoding, "concatenate_clips") as concat:
            self._encode()

        self.assertEqual(run_ffmpeg.call_count, 5)  # lead, in, static, out, trail
        phases_arg = concat.call_args.args[0]
        self.assertEqual(
            [p.rsplit("_", 1)[-1] for p in phases_arg],
            ["lead.mp4", "in.mp4", "static.mp4", "out.mp4", "trail.mp4"],
        )

    def test_a_failing_phase_still_cleans_up_every_phase_file_and_raises(self):
        def _fake_run_ffmpeg(build_cmd, codec, args, error_label):
            if build_cmd is encoding._build_encode_cmd:  # the static phase
                raise RuntimeError(f"{error_label} failed (ffmpeg exit 1): boom")

        with mock.patch.object(encoding, "_run_ffmpeg", side_effect=_fake_run_ffmpeg), \
             mock.patch.object(encoding, "concatenate_clips") as concat:
            with self.assertRaises(RuntimeError):
                self._encode()

        concat.assert_not_called()
        # Every phase gets a cleanup attempt, not just the ones declared
        # before the failing one - concurrent phases can finish (and leave
        # a file on disk) in any order relative to each other's failure.
        self.assertEqual(self.remove_mock.call_count, 5)


class SafeFilenameTest(unittest.TestCase):
    def test_slugifies_and_truncates(self):
        self.assertEqual(encoding.safe_filename("Hello, World!"), "hello_world")
        self.assertLessEqual(len(encoding.safe_filename("x" * 200)), 50)

    def test_non_ascii_titles_still_produce_a_usable_slug(self):
        self.assertNotIn("/", encoding.safe_filename("설탕 허니 아이스티"))


if __name__ == "__main__":
    unittest.main()
