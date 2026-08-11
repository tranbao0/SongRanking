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
import subprocess

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
        # download_song commits + ensure_raw_quality after the ladder;
        # keep these tests focused on the first fetch / silence shift.
        self._ensure = mock.patch.object(
            encoding, "ensure_raw_quality",
            side_effect=lambda rank, url, start, end, raw_clip, min_height=None: raw_clip,
        )
        self._ensure.start()
        self.addCleanup(self._ensure.stop)
        self._commit = mock.patch.object(
            encoding, "commit_raw_clip",
            side_effect=lambda src, video_id: src,
        )
        self._commit.start()
        self.addCleanup(self._commit.stop)

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
            result.stdout = ""
            result.stderr = ""
            return result
        return _make

    def test_successful_download_seeds_the_cache(self):
        with mock.patch.object(encoding.subprocess, "run", side_effect=self._completed(0)), \
             mock.patch.object(encoding, "_probe_duration", return_value=15.0) as probe, \
             mock.patch.object(encoding, "_audio_covers_clip", return_value=True), \
             mock.patch.object(encoding, "throttle"), \
             mock.patch.object(encoding, "_leading_silence_duration", return_value=0.0), \
             mock.patch.object(encoding, "_trailing_silence_duration", return_value=0.0):
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
             mock.patch.object(encoding, "_probe_duration", side_effect=[None, 15.0]) as probe, \
             mock.patch.object(encoding, "_audio_covers_clip", return_value=True), \
             mock.patch.object(encoding, "throttle"), \
             mock.patch.object(encoding, "_leading_silence_duration", return_value=0.0), \
             mock.patch.object(encoding, "_trailing_silence_duration", return_value=0.0):
            raw = encoding.download_song(2, "https://example.com/watch?v=bbbbbbbbbbb")

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(encoding._duration_cache[raw], 15.0)

    def test_download_failing_both_attempts_caches_nothing(self):
        with mock.patch.object(encoding.subprocess, "run", side_effect=self._completed(1)), \
             mock.patch.object(encoding, "_probe_duration", return_value=None), \
             mock.patch.object(encoding, "throttle"), \
             mock.patch.object(encoding, "cookie_args", return_value=["--cookies", "cookies.txt"]):
            with self.assertRaises(RuntimeError):
                encoding.download_song(3, "https://example.com/watch?v=ccccccccccc")

        self.assertEqual(encoding._duration_cache, {})

    def test_failed_silence_shift_does_not_clobber_the_original_raw(self):
        """
        Regression for rank-16 FAKE LOVE: a failed leading-silence re-download
        used to --force-overwrite the same raw path, wipe the good file, then
        still return that path ("keeping original clip"). Encode later died
        with "No such file or directory".
        """
        url = "https://example.com/watch?v=7C2z4GqqS5E"
        raw = encoding.cached_clip_path(url)
        written = []

        def _run(argv, **kwargs):
            # argv is the full ytdlp_cmd list; -o is followed by the dest path.
            out = argv[argv.index("-o") + 1]
            written.append(out)
            result = mock.Mock()
            # First call (canonical raw) succeeds; shift-to-tmp fails.
            result.returncode = 0 if out == raw else 1
            result.stderr = ""
            return result

        with mock.patch.object(encoding.subprocess, "run", side_effect=_run), \
             mock.patch.object(encoding, "_probe_duration", return_value=20.0), \
             mock.patch.object(encoding, "_audio_covers_clip", return_value=True), \
             mock.patch.object(encoding, "throttle"), \
             mock.patch.object(encoding, "_leading_silence_duration", return_value=0.9), \
             mock.patch.object(encoding, "_trailing_silence_duration", return_value=0.0), \
             mock.patch.object(encoding.os, "replace") as replace, \
             mock.patch.object(encoding.os.path, "exists", return_value=False), \
             mock.patch.object(encoding.os, "remove"):
            out = encoding.download_song(16, url, start="18.10", end="38.10")

        self.assertEqual(out, raw)
        self.assertEqual(written[0], raw)
        self.assertTrue(written[1].endswith(".shift.tmp.mp4"))
        replace.assert_not_called()
        # Original duration memo must still be the successful first download's.
        self.assertEqual(encoding._duration_cache[raw], 20.0)

    def test_fallback_walks_quality_clients_before_android_then_cookies(self):
        """
        Default → mweb → web_embedded → android → cookies.
        android stays last among player clients (360p); cookies are last overall.
        """
        from shared.ytdlp_throttle import DOWNLOAD_FALLBACK_CLIENTS
        import sys

        clients = list(DOWNLOAD_FALLBACK_CLIENTS)
        calls = []
        timeouts = []

        def _capture(*args, **kwargs):
            calls.append(list(args[0]))
            timeouts.append(kwargs.get("timeout"))
            return self._completed(1)()

        with mock.patch.object(encoding.subprocess, "run", side_effect=_capture), \
             mock.patch.object(encoding, "_probe_duration", return_value=None), \
             mock.patch.object(encoding, "cookie_args", return_value=["--cookies", "cookies.txt"]), \
             mock.patch.object(encoding, "throttle"), \
             mock.patch.object(encoding, "download_fallback_clients", return_value=clients):
            with self.assertRaises(RuntimeError):
                encoding.download_song(4, "https://example.com/watch?v=ddddddddddd")

        # default + each fallback client + cookies
        self.assertEqual(len(calls), 1 + len(clients) + 1)
        client_args = []
        for argv in calls[1:-1]:
            self.assertIn("player_client=", " ".join(argv))
            for tok in argv:
                if tok.startswith("youtube:player_client="):
                    client_args.append(tok.split("=", 1)[1])
                    break
        self.assertEqual(client_args, clients)
        self.assertIn("--cookies", calls[-1])
        self.assertEqual(calls[0][:3], [sys.executable, "-m", "yt_dlp"])
        self.assertIn("--remote-components", calls[0])
        self.assertIn("ejs:github", calls[0])
        self.assertTrue(all(t == encoding.DOWNLOAD_ATTEMPT_TIMEOUT for t in timeouts))

    def test_fallback_skips_mweb_when_pot_provider_is_down(self):
        calls = []

        def _capture(*args, **kwargs):
            calls.append(list(args[0]))
            return self._completed(1)()

        with mock.patch.object(encoding.subprocess, "run", side_effect=_capture), \
             mock.patch.object(encoding, "_probe_duration", return_value=None), \
             mock.patch.object(encoding, "cookie_args", return_value=["--cookies", "cookies.txt"]), \
             mock.patch.object(encoding, "throttle"), \
             mock.patch.object(encoding, "download_fallback_clients",
                               return_value=["web_embedded", "android"]):
            with self.assertRaises(RuntimeError):
                encoding.download_song(5, "https://example.com/watch?v=eeeeeeeeeee")

        client_args = []
        for argv in calls[1:-1]:
            for tok in argv:
                if tok.startswith("youtube:player_client="):
                    client_args.append(tok.split("=", 1)[1])
                    break
        self.assertEqual(client_args, ["web_embedded", "android"])
        self.assertNotIn("mweb", client_args)

    def test_attempt_timeout_advances_to_the_next_fallback(self):
        """A hung first attempt must not block the ladder - TimeoutExpired → next client."""
        calls = []

        def _side_effect(argv, **kwargs):
            calls.append(list(argv))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 60))
            return self._completed(0)()

        with mock.patch.object(encoding.subprocess, "run", side_effect=_side_effect), \
             mock.patch.object(encoding, "_probe_duration", return_value=15.0), \
             mock.patch.object(encoding, "_audio_covers_clip", return_value=True), \
             mock.patch.object(encoding, "throttle"), \
             mock.patch.object(encoding, "_leading_silence_duration", return_value=0.0), \
             mock.patch.object(encoding, "_trailing_silence_duration", return_value=0.0), \
             mock.patch.object(encoding, "download_fallback_clients", return_value=["android"]):
            raw = encoding.download_song(6, "https://example.com/watch?v=fffffffffff")

        self.assertEqual(len(calls), 2)
        self.assertTrue(any("player_client=android" in tok for tok in calls[1]))
        self.assertEqual(encoding._duration_cache[raw], 15.0)


class RawClipCacheTest(unittest.TestCase):
    """
    Guards the raw-clip cache keying: by video ID (stable across runs),
    not by rank/title (which shift as the chart re-ranks) - see
    RAW_CACHE_DIR and pipeline.py's _download. Final files are
    `{id}_{height}p.mp4`; staging uses `{id}.download.tmp.mp4`.
    """

    def test_staging_path_is_keyed_by_video_id_not_rank_or_title(self):
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        self.assertEqual(
            encoding.cached_clip_path(url),
            f"{encoding.RAW_CACHE_DIR}/aaaaaaaaaaa.download.tmp.mp4",
        )
        self.assertEqual(
            encoding.raw_named_path("aaaaaaaaaaa", 1080),
            f"{encoding.RAW_CACHE_DIR}/aaaaaaaaaaa_1080p.mp4",
        )

    def test_same_video_different_urls_share_one_cache_path(self):
        watch_url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        short_url = "https://youtu.be/aaaaaaaaaaa"
        self.assertEqual(encoding.cached_clip_path(watch_url), encoding.cached_clip_path(short_url))

    def test_cached_clip_is_none_when_nothing_is_downloaded_yet(self):
        with mock.patch.object(encoding.os.path, "isdir", return_value=False):
            self.assertIsNone(encoding.cached_clip("https://www.youtube.com/watch?v=aaaaaaaaaaa"))

    def test_cached_clip_returns_the_tallest_named_file(self):
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        named = encoding.raw_named_path("aaaaaaaaaaa", 1080)
        with mock.patch.object(encoding, "list_raw_candidates", return_value=[named]), \
             mock.patch.object(encoding, "_cached_duration", return_value=15.0), \
             mock.patch.object(encoding, "_audio_covers_clip", return_value=True), \
             mock.patch.object(encoding, "probe_height", return_value=1080), \
             mock.patch.object(encoding, "height_from_raw_path", return_value=1080):
            self.assertEqual(encoding.cached_clip(url), named)

    def test_cached_clip_migrates_legacy_to_height_name(self):
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        legacy = encoding.legacy_raw_path("aaaaaaaaaaa")
        named = encoding.raw_named_path("aaaaaaaaaaa", 720)
        with mock.patch.object(encoding, "list_raw_candidates", return_value=[legacy]), \
             mock.patch.object(encoding, "_cached_duration", return_value=15.0), \
             mock.patch.object(encoding, "_audio_covers_clip", return_value=True), \
             mock.patch.object(encoding, "probe_height", return_value=720), \
             mock.patch.object(encoding, "commit_raw_clip", return_value=named) as commit:
            self.assertEqual(encoding.cached_clip(url), named)
            commit.assert_called_once_with(legacy, "aaaaaaaaaaa")

    def test_cached_clip_is_none_when_the_file_is_corrupt(self):
        """A stale/corrupt cache entry (e.g. an interrupted prior run) must
        not be trusted just because it exists - see cached_clip's docstring."""
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        named = encoding.raw_named_path("aaaaaaaaaaa", 360)
        with mock.patch.object(encoding, "list_raw_candidates", return_value=[named]), \
             mock.patch.object(encoding, "_cached_duration", return_value=None):
            self.assertIsNone(encoding.cached_clip(url))


class RawWindowSidecarTest(unittest.TestCase):
    def test_write_and_read_round_trip(self):
        raw = encoding.raw_named_path("fakevid", 1080)
        written = {}

        class _FakeFile:
            def __init__(self, buf):
                self._buf = buf
            def write(self, data):
                self._buf.append(data)
            def read(self):
                return "".join(self._buf)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_open(path, mode="r", encoding=None):
            if "w" in mode:
                written[path] = []
                return _FakeFile(written[path])
            return _FakeFile(written[path])

        with mock.patch.object(encoding, "open", fake_open), \
             mock.patch.object(encoding.os, "makedirs"), \
             mock.patch.object(encoding.os.path, "exists", side_effect=lambda p: p in written):
            encoding.write_raw_window(raw, "12.50", "32.50")
            self.assertEqual(
                encoding.raw_window_path(raw),
                f"{encoding.RAW_CACHE_DIR}/fakevid.window",
            )
            self.assertEqual(encoding.read_raw_window(raw), ("12.50", "32.50"))


class EnsureRawQualityTest(unittest.TestCase):
    def setUp(self):
        encoding._duration_cache.clear()
        self.addCleanup(encoding._duration_cache.clear)
        self.video_id = "ggggggggggg"
        self.url = f"https://example.com/watch?v={self.video_id}"
        self.raw = encoding.raw_named_path(self.video_id, 360)

    def test_skips_upgrade_when_already_tall_enough(self):
        tall = encoding.raw_named_path(self.video_id, 1440)
        with mock.patch.object(encoding, "write_raw_window") as write_win, \
             mock.patch.object(encoding, "probe_height", return_value=1440), \
             mock.patch.object(encoding, "_download_section") as download, \
             mock.patch.object(encoding, "commit_raw_clip") as commit:
            out = encoding.ensure_raw_quality(
                1, self.url, "10.00", "30.00", tall, min_height=1440,
            )
        self.assertEqual(out, tall)
        write_win.assert_called_once_with(tall, "10.00", "30.00")
        download.assert_not_called()
        commit.assert_not_called()

    def test_upgrades_via_quality_clients_never_android(self):
        """360p cache → taller fetch; android must not appear in the ladder."""
        clients_seen = []
        upgraded = encoding.raw_named_path(self.video_id, 1080)

        def probe(path):
            return 1080 if path.endswith(".upgrade.tmp.mp4") else 360

        def download(url, dest, start, end, extra_args, seed_duration_for=None):
            for tok in extra_args:
                if tok.startswith("youtube:player_client="):
                    clients_seen.append(tok.split("=", 1)[1])
            result = mock.Mock()
            result.returncode = 0 if any(
                t.startswith("youtube:player_client=") for t in extra_args
            ) else 1
            result.stderr = ""
            return result

        with mock.patch.object(encoding, "write_raw_window"), \
             mock.patch.object(encoding, "probe_height", side_effect=probe), \
             mock.patch.object(encoding, "_download_section", side_effect=download), \
             mock.patch.object(encoding, "quality_upgrade_clients",
                               return_value=["mweb", "web_embedded"]), \
             mock.patch.object(encoding, "commit_raw_clip", return_value=upgraded) as commit, \
             mock.patch.object(encoding.os.path, "exists", return_value=False), \
             mock.patch.object(encoding.os, "remove"):
            out = encoding.ensure_raw_quality(
                7, self.url, "10.00", "30.00", self.raw, min_height=1080,
            )

        self.assertEqual(out, upgraded)
        self.assertEqual(clients_seen, ["mweb"])
        self.assertNotIn("android", clients_seen)
        commit.assert_called()

    def test_keeps_original_when_upgrade_attempts_fail(self):
        with mock.patch.object(encoding, "write_raw_window"), \
             mock.patch.object(encoding, "probe_height", return_value=360), \
             mock.patch.object(encoding, "_download_section") as download, \
             mock.patch.object(encoding, "quality_upgrade_clients", return_value=["mweb"]), \
             mock.patch.object(encoding, "cookie_args", return_value=["--cookies", "c.txt"]), \
             mock.patch.object(encoding, "commit_raw_clip") as commit, \
             mock.patch.object(encoding.os.path, "exists", return_value=False), \
             mock.patch.object(encoding.os, "remove"):
            download.return_value = mock.Mock(returncode=1, stderr="")
            out = encoding.ensure_raw_quality(
                8, self.url, "10.00", "30.00", self.raw, min_height=1080,
            )

        self.assertEqual(out, self.raw)
        self.assertEqual(download.call_count, 3)
        commit.assert_not_called()


class EncodeSongSinglePassTest(unittest.TestCase):
    """
    Guards encode_song's one-pass video encode + continuous audio mux:
    one ffmpeg video graph for the whole timeline (see build_song_filter_
    complex), then a mux that matches audio to the probed video duration.
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
        p3 = mock.patch.object(encoding, "_probe_video_duration", return_value=18.0)
        p3.start(); self.addCleanup(p3.stop)
        p4 = mock.patch.object(encoding.os.path, "exists", return_value=True)
        p4.start(); self.addCleanup(p4.stop)
        p5 = mock.patch.object(encoding.os, "remove")
        self.remove_mock = p5.start()
        self.addCleanup(p5.stop)

    def _encode(self, **overrides):
        kwargs = dict(
            style=self.style, raw_clip="raw.mp4", rank=1, title="Song", artist="Artist",
            peak=1, entry_type="", views=100, release_date="2026.01.01",
            months_on_chart=1, head_trim=0.0, tail_trim=0.0, clips_dir="clips",
        )
        kwargs.update(overrides)
        return encoding.encode_song(**kwargs)

    def test_encodes_video_once_then_muxes_audio_to_probed_duration(self):
        with mock.patch.object(encoding, "_run_ffmpeg") as run_ffmpeg:
            self._encode(head_trim=1.0, tail_trim=1.0)

        self.assertEqual(run_ffmpeg.call_count, 2)
        video_call, mux_call = run_ffmpeg.call_args_list
        self.assertIs(video_call.args[0], encoding._build_song_video_cmd)
        self.assertIs(mux_call.args[0], encoding._build_mux_audio_cmd)
        # Interior clip: no lead/trail bare; window is full_dur - trims = 18s.
        # Mux gets (video_only, raw, window_start=1.0, source_dur=18.0, video_dur=18.0, final).
        mux_args = mux_call.args[2]
        self.assertEqual(mux_args[2], 1.0)   # audio_start
        self.assertEqual(mux_args[3], 18.0)  # source_audio_duration
        self.assertEqual(mux_args[4], 18.0)  # probed video_duration

    def test_edge_clip_passes_lead_and_trail_bare_into_video_cmd(self):
        with mock.patch.object(encoding, "_run_ffmpeg") as run_ffmpeg:
            self._encode(head_trim=0.0, tail_trim=0.0)

        video_args = run_ffmpeg.call_args_list[0].args[2]
        # (... overlay_duration, lead_bare, trail_bare, out_path)
        self.assertEqual(video_args[10], 1.0)  # lead_bare == boundary_duration
        self.assertEqual(video_args[11], 1.0)  # trail_bare

    def test_a_failing_video_encode_still_cleans_up_and_raises(self):
        def _fake_run_ffmpeg(build_cmd, codec, args, error_label):
            if build_cmd is encoding._build_song_video_cmd:
                raise RuntimeError(f"{error_label} failed (ffmpeg exit 1): boom")

        with mock.patch.object(encoding, "_run_ffmpeg", side_effect=_fake_run_ffmpeg):
            with self.assertRaises(RuntimeError):
                self._encode()

        # video_only + overlay_png both get a cleanup attempt
        self.assertGreaterEqual(self.remove_mock.call_count, 2)


class SafeFilenameTest(unittest.TestCase):
    def test_slugifies_and_truncates(self):
        self.assertEqual(encoding.safe_filename("Hello, World!"), "hello_world")
        self.assertLessEqual(len(encoding.safe_filename("x" * 200)), 50)

    def test_non_ascii_titles_still_produce_a_usable_slug(self):
        self.assertNotIn("/", encoding.safe_filename("설탕 허니 아이스티"))


if __name__ == "__main__":
    unittest.main()
