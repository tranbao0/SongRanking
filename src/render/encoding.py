"""
Download + encode stage: pulling clips with yt-dlp, burning in overlays with
ffmpeg, and concatenating the final compilation. Split out of pipeline.py so
the orchestration logic (main run flow) isn't tangled with subprocess/ffmpeg
plumbing.
"""

import subprocess
import os
import re
import threading

from render.overlay import (
    build_overlay_image, build_song_filter_complex, build_transition_filter_complex,
)
from shared.youtube_api import extract_video_id
from shared.ytdlp_throttle import (
    download_fallback_clients, quality_upgrade_clients, DOWNLOAD_ATTEMPT_TIMEOUT,
    throttle, cookie_args, ytdlp_cmd, pot_extractor_args,
)

CLIPS_DIR = "assets/clips"

# Raw clips are cached here permanently, keyed by video ID rather than by
# chart rank (which shifts run to run) - so a song still on the chart next
# time reuses the exact same clip instead of re-downloading it, and reuses
# the exact same window rather than re-rolling heatmap.pick_clip's random
# choice among hot sections (see pipeline.py's _download). Filenames are
# `{video_id}_{height}p.mp4` (legacy `{video_id}.mp4` is still accepted and
# renamed on touch). Never evicted: a 15s clip is small even at thousands
# of songs.
RAW_CACHE_DIR = f"{CLIPS_DIR}/raw"

# Floor for raw-clip video height. Below this, ensure_raw_quality retries
# with quality-preserving clients (never android - that caps at ~360p) and
# keeps the taller file if one lands. Override with YTDLP_MIN_RAW_HEIGHT;
# pipeline also passes the style canvas height so a 1440p canvas raises the
# floor automatically. Default 1440 matches the 2560x1440 style canvas.
MIN_RAW_HEIGHT = int(os.environ.get("YTDLP_MIN_RAW_HEIGHT", "1440"))

_print_lock = threading.Lock()


def _log(msg):
    """Thread-safe print - avoids interleaved/garbled lines when songs process concurrently."""
    with _print_lock:
        print(msg)


def safe_filename(text, max_len=50):
    s = re.sub(r"[^\w]+", "_", text.lower()).strip("_")
    return s[:max_len]


_HW_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf")

_HW_ENCODE_ARGS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "22", "-b:v", "0", "-bf", "0"],
    "h264_qsv":   ["-c:v", "h264_qsv", "-preset", "fast", "-global_quality", "22", "-bf", "0"],
    "h264_amf":   ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22", "-bf", "0"],
}
_CPU_ENCODE_ARGS = ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-bf", "0"]


def detect_hw_encoder():
    """
    Probe the local ffmpeg build for a usable hardware H.264 encoder
    (NVENC > QSV > AMF). Returns the codec name, or None if only software
    encoding is available. Availability in the ffmpeg build doesn't
    guarantee the hardware actually works at runtime - encode_song()
    falls back to libx264 per-clip if the hardware encode fails.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for codec in _HW_ENCODERS:
        if codec in result.stdout:
            return codec
    return None


def _run_ffmpeg(build_cmd, codec, args, error_label):
    """
    Runs an ffmpeg command built by `build_cmd(codec, *args)`, retrying once
    on software encoding if a GPU encode was requested and failed. Shared by
    every encode_* function below so the GPU->CPU fallback logic lives in
    one place.
    """
    result = subprocess.run(build_cmd(codec, *args), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 and codec:
        result = subprocess.run(build_cmd(None, *args), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"{error_label} failed (ffmpeg exit {result.returncode}): {result.stderr[-500:]}")


def _trim_input_args(raw_clip, trim_start=0.0, trim_duration=None):
    # Trimming is an input seek (before -i), not a filter: it's frame-accurate
    # by default in modern ffmpeg and lets the filter graphs below stay
    # identical regardless of which window of the clip they're fed.
    args = []
    if trim_start:
        args += ["-ss", str(trim_start)]
    if trim_duration is not None:
        args += ["-t", str(trim_duration)]
    return [*args, "-i", raw_clip]


# Song video is encoded without audio (-an); encode_song muxes one continuous
# audio track afterward (see _build_mux_audio_cmd). Fragmenting a clip's
# soundtrack across separately-encoded pieces and stitching them produces
# audible AAC seams at every boundary - and those boundaries land exactly
# when the overlay wipes on or off.
#
# Transition clips still carry their own audio (acrossfade inside the
# filter graph). Their trailing output `-t` caps whichever stream ran long
# after CFR retiming / acrossfade, so concatenate_clips doesn't have to
# pad a few dozen ms of near-silence at each song/transition seam.
def _build_song_video_cmd(codec, raw_clip, overlay_png, cw, ch, fps, trim_start, trim_duration,
                          overlay_enter, overlay_exit, overlay_duration, lead_bare, trail_bare,
                          out_path):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    return [
        "ffmpeg",
        *_trim_input_args(raw_clip, trim_start, trim_duration),
        "-loop", "1", "-t", str(trim_duration), "-i", overlay_png,
        "-filter_complex", build_song_filter_complex(
            cw, ch, fps, overlay_enter, overlay_exit, overlay_duration,
            lead_bare, trail_bare, trim_duration,
        ),
        "-map", "[vout]", "-an",
        *video_args,
        "-r", str(fps), "-fps_mode", "cfr",
        "-y", out_path,
    ]


# Player-client fallbacks live in shared.ytdlp_throttle
# (mweb → web_embedded → android; mweb skipped when POT is down). See
# download_fallback_clients() / DOWNLOAD_ATTEMPT_TIMEOUT for the live list
# and the per-attempt soft ceiling that keeps a hung client from stalling
# the whole download pool.

# The cookies tier deliberately does NOT pin a player_client the way the
# android tier does: cookies are only reached after the default rotation
# and quality-preserving clients already failed, and letting yt-dlp pick
# from its full client list (now including tv, which serves adaptive
# formats to authenticated sessions without a PO token) recovers full
# resolution. Pinning to a single cookie-compatible client like mweb
# forces every cookies-tier download onto that client's progressive-only
# format 18 (360p) even when a better client would have worked - verified
# against live cache: every clip previously downloaded via a pinned
# cookies client came out 640x360.


def _probe_duration(path):
    """Always spawns ffprobe. Use _cached_duration for files known to be final."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


# encode_song reads audio across a raw clip's whole window, right up to
# its tail (and edge clips include their bare bookends), so a clip whose
# audio track is drastically shorter than its video - seen on some yt-dlp
# downloads, e.g. a botched video+audio merge where one track truncates
# early - looks fine by _probe_duration (that reads the container's
# overall/video-driven duration) but makes ffmpeg's AAC decoder choke with
# "Decode error rate ... exceeds maximum" the moment a seek goes past
# where the real audio data ends. That's a hard ffmpeg failure, not
# something a later encode step can route around, so it has to be caught
# here instead: at least this much of the clip's own duration must
# actually have audio behind it. Allows some slack (not 1.0) since a
# handful of source clips genuinely end on a few frames of trailing
# silence the container still counts as covered.
_MIN_AUDIO_COVERAGE = 0.9


def _probe_audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _audio_covers_clip(path, duration):
    """Whether `path`'s audio track spans (most of) its `duration`-long video."""
    audio_duration = _probe_audio_duration(path)
    if audio_duration is None:
        return False
    return audio_duration >= duration * _MIN_AUDIO_COVERAGE


# A raw clip's duration gets asked for repeatedly once it's on disk: by
# encode_song, then again by each transition segment built against it, and
# again whenever _run_ffmpeg rebuilds a command for its CPU-fallback retry.
# Every one of those is a process spawn (~370ms on the dev machine) for a
# number that can't change, since nothing rewrites a clip after download.
#
# Deliberately not used by download_song: it probes to decide whether an
# attempt produced a usable file at all, and a retry rewrites that same
# path - so it probes uncached and seeds this only once a file is final.
_duration_cache: dict[str, float] = {}
_duration_cache_lock = threading.Lock()


def _cached_duration(path):
    """Duration of a file that won't be rewritten, probed at most once."""
    with _duration_cache_lock:
        cached = _duration_cache.get(path)
    if cached is not None:
        return cached

    duration = _probe_duration(path)
    if duration is not None:
        with _duration_cache_lock:
            _duration_cache[path] = duration
    return duration


def raw_staging_path(video_id: str) -> str:
    """In-progress download target; renamed to `{id}_{height}p.mp4` on commit."""
    return f"{RAW_CACHE_DIR}/{video_id}.download.tmp.mp4"


def raw_named_path(video_id: str, height: int) -> str:
    """Final cache filename including probed height, e.g. `abc123_1080p.mp4`."""
    return f"{RAW_CACHE_DIR}/{video_id}_{int(height)}p.mp4"


def legacy_raw_path(video_id: str) -> str:
    """Pre-height naming (`{id}.mp4`); still accepted and migrated on touch."""
    return f"{RAW_CACHE_DIR}/{video_id}.mp4"


def cached_clip_path(url: str) -> str:
    """
    Staging path for a fresh download of `url`. Final on-disk name includes
    height after commit_raw_clip; use cached_clip() to find an existing file.
    """
    return raw_staging_path(extract_video_id(url))


_RAW_HEIGHT_NAME_RE = re.compile(r"^(.+)_(\d+)p\.mp4$", re.IGNORECASE)


def video_id_from_raw_path(path: str) -> str:
    """Recover the YouTube video id from a raw/staging/legacy filename."""
    base = os.path.basename(path)
    for suffix in (".download.tmp.mp4", ".upgrade.tmp.mp4", ".shift.tmp.mp4"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    m = _RAW_HEIGHT_NAME_RE.match(base)
    if m:
        return m.group(1)
    if base.lower().endswith(".mp4"):
        return base[:-4]
    return base


def height_from_raw_path(path: str) -> int | None:
    """Height encoded in `{id}_{height}p.mp4`, or None for legacy/staging."""
    m = _RAW_HEIGHT_NAME_RE.match(os.path.basename(path))
    return int(m.group(2)) if m else None


def raw_window_path_for_id(video_id: str) -> str:
    """Stable sidecar keyed by video id (survives height renames)."""
    return f"{RAW_CACHE_DIR}/{video_id}.window"


def raw_window_path(raw_clip: str) -> str:
    """Sidecar for this raw clip's video id (preferred) or legacy beside-file."""
    return raw_window_path_for_id(video_id_from_raw_path(raw_clip))


def write_raw_window(raw_clip: str, start, end) -> None:
    """Persist the section window so a later quality-upgrade can re-fetch it."""
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    with open(raw_window_path(raw_clip), "w", encoding="utf-8") as f:
        f.write(f"{start}\n{end}\n")


def read_raw_window(raw_clip: str) -> tuple[str, str] | None:
    """(start, end) written by write_raw_window, or None if missing/corrupt."""
    candidates = [raw_window_path(raw_clip)]
    # Legacy beside-file sidecar from before id-keyed windows.
    beside = f"{raw_clip}.window"
    if beside not in candidates:
        candidates.append(beside)
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
            if len(lines) >= 2:
                return lines[0], lines[1]
        except OSError:
            continue
    return None


def probe_height(path: str) -> int | None:
    """Video stream height in pixels, or None if unreadable."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=height",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return int(float(result.stdout.strip().splitlines()[0]))
    except (ValueError, IndexError):
        return None


def list_raw_candidates(video_id: str) -> list[str]:
    """
    On-disk raws for `video_id`, tallest first. Includes `{id}_{h}p.mp4` and
    legacy `{id}.mp4`. Staging/tmp files are ignored.
    """
    if not os.path.isdir(RAW_CACHE_DIR):
        return []
    found: list[str] = []
    legacy = legacy_raw_path(video_id)
    if os.path.exists(legacy):
        found.append(legacy)
    prefix = f"{video_id}_"
    try:
        names = os.listdir(RAW_CACHE_DIR)
    except OSError:
        return found
    for name in names:
        if not name.startswith(prefix):
            continue
        m = _RAW_HEIGHT_NAME_RE.match(name)
        if m and m.group(1) == video_id:
            found.append(f"{RAW_CACHE_DIR}/{name}")

    def sort_key(path: str) -> int:
        named = height_from_raw_path(path)
        if named is not None:
            return named
        return probe_height(path) or 0

    found.sort(key=sort_key, reverse=True)
    return found


def _forget_duration(path: str) -> None:
    with _duration_cache_lock:
        _duration_cache.pop(path, None)


def _remove_raw_variant(path: str) -> None:
    _forget_duration(path)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    # Legacy beside-file window only - id-keyed window is shared.
    beside = f"{path}.window"
    try:
        if os.path.exists(beside):
            os.remove(beside)
    except OSError:
        pass


def commit_raw_clip(src_path: str, video_id: str) -> str:
    """
    Move `src_path` to `{video_id}_{height}p.mp4` and drop other variants for
    the same id so the cache always has one canonical raw per video.
    """
    height = probe_height(src_path)
    if height is None or height <= 0:
        dest = legacy_raw_path(video_id)
    else:
        dest = raw_named_path(video_id, height)

    if os.path.abspath(src_path) != os.path.abspath(dest):
        # Drop an existing dest first so os.replace can't fail on Windows
        # when replacing across "same name already taken by another handle"
        # edge cases after a prior partial upgrade.
        if os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(src_path):
            _remove_raw_variant(dest)
        os.makedirs(RAW_CACHE_DIR, exist_ok=True)
        os.replace(src_path, dest)
        _forget_duration(src_path)

    duration = _probe_duration(dest)
    if duration is not None:
        with _duration_cache_lock:
            _duration_cache[dest] = duration

    for other in list_raw_candidates(video_id):
        if os.path.abspath(other) != os.path.abspath(dest):
            _remove_raw_variant(other)
    return dest


def cached_clip(url: str) -> str | None:
    """
    The best cached raw clip for `url` if one exists and still probes as a
    real clip, else None. Prefers the tallest `{id}_{height}p.mp4`; migrates
    a legacy `{id}.mp4` into the height-named form on first successful hit.
    """
    video_id = extract_video_id(url)
    for path in list_raw_candidates(video_id):
        duration = _cached_duration(path)
        if duration is None or not _audio_covers_clip(path, duration):
            continue
        named_h = height_from_raw_path(path)
        probed_h = probe_height(path)
        if probed_h is not None and (named_h is None or named_h != probed_h):
            return commit_raw_clip(path, video_id)
        return path
    return None


def _download_section(url, dest, window_start, window_end, extra_args, *, seed_duration_for=None):
    """
    One yt-dlp --download-sections attempt into `dest`. Returns a
    CompletedProcess; on "success" that still fails duration/audio
    postchecks, returncode is forced to 1. When seed_duration_for is set
    and the attempt is good, that path's duration memo is updated.
    """
    throttle()
    try:
        result = subprocess.run(ytdlp_cmd(
            "--download-sections", f"*{window_start}-{window_end}",
            # No h264 preference: YouTube doesn't serve h264 above 1080p at
            # all, so ranking it before ext left h264-less 1440p/2160p
            # candidates resolved by yt-dlp's internal vp9-over-av01
            # tiebreak - i.e. the vp9/webm formats a raw_clip (fixed .mp4
            # path) can't hold, which yt-dlp then silently merges into a
            # `.mkv`-suffixed file instead. ext:mp4:m4a first guarantees an
            # mp4-native pick (h264 up to 1080p, av01 above it) at every
            # resolution so the output always lands at the .mp4 path below.
            "-S", "ext:mp4:m4a,res:2160",
            *pot_extractor_args(),
            *extra_args,
            "--no-playlist",
            # A stale file can already sit at dest - e.g. a corrupt/
            # mismatched-merge clip caught by the audio-coverage postcheck
            # below on a previous run, or one killed mid-download - and
            # yt-dlp skips redownloading onto an existing destination path
            # by default. Every attempt needs to actually overwrite it.
            "--force-overwrites",
            "-o", dest,
            url,
        ), capture_output=True, text=True, encoding="utf-8", errors="replace",
           timeout=DOWNLOAD_ATTEMPT_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        # Soft-fail into the next fallback client instead of letting one
        # hung negotiation pin a download worker for minutes.
        raw_err = e.stderr or ""
        if isinstance(raw_err, bytes):
            raw_err = raw_err.decode("utf-8", errors="replace")
        result = subprocess.CompletedProcess(
            list(e.cmd or []), returncode=1,
            stdout="",
            stderr=raw_err + (
                f"\n(timeout) yt-dlp exceeded {DOWNLOAD_ATTEMPT_TIMEOUT:.0f}s "
                "- treating as a failed attempt"
            ),
        )
    # yt-dlp can exit 0 even when the section download itself produced
    # nothing usable - e.g. a video bucketed into a player-client
    # experiment whose adaptive-format URLs reject the byte-range
    # requests --download-sections relies on. ffmpeg then writes an
    # empty file while yt-dlp still reports success, so exit code
    # alone isn't a reliable success signal - the output has to
    # actually probe as a real clip too.
    if result.returncode == 0:
        duration = _probe_duration(dest)
        if duration is None:
            result.returncode = 1
            result.stderr += "\n(postcheck) downloaded file has no readable duration - treating as a failed attempt"
        elif not _audio_covers_clip(dest, duration):
            result.returncode = 1
            result.stderr += "\n(postcheck) downloaded file's audio track doesn't cover its video duration (corrupt/mismatched merge) - treating as a failed attempt"
        elif seed_duration_for is not None:
            with _duration_cache_lock:
                _duration_cache[seed_duration_for] = duration
    return result


def ensure_raw_quality(rank, url, start, end, raw_clip, min_height=None):
    """
    If `raw_clip` is shorter than `min_height` (default MIN_RAW_HEIGHT),
    re-fetch the same section via quality-preserving clients and replace
    the cache only when the new file is taller. Never tries `android`
    (360p). Always writes the window sidecar so a later run can upgrade
    without re-rolling the heatmap. Returns the canonical height-named path.
    """
    if min_height is None:
        min_height = MIN_RAW_HEIGHT
    video_id = extract_video_id(url)
    write_raw_window(raw_clip, start, end)

    height = probe_height(raw_clip)
    if height is None:
        return raw_clip
    # Normalize naming even when already tall enough (legacy → `{id}_{h}p`).
    if height_from_raw_path(raw_clip) != height:
        raw_clip = commit_raw_clip(raw_clip, video_id)
    if height >= min_height:
        return raw_clip

    original_height = height
    _log(
        f"  [rank {rank}] Cached clip is {height}p (< {min_height}p) - "
        "trying quality upgrade..."
    )
    tmp_path = f"{RAW_CACHE_DIR}/{video_id}.upgrade.tmp.mp4"

    def _try_upgrade(extra_args, label):
        nonlocal height, raw_clip
        result = _download_section(url, tmp_path, start, end, extra_args)
        if result.returncode != 0:
            _log(f"  [rank {rank}] {label} upgrade failed")
            return False
        new_h = probe_height(tmp_path)
        if new_h is None or new_h <= height:
            _log(
                f"  [rank {rank}] {label} upgrade "
                f"{'unreadable' if new_h is None else f'{new_h}p'} "
                f"(not taller than {height}p) - discarding"
            )
            return False
        raw_clip = commit_raw_clip(tmp_path, video_id)
        height = new_h
        _log(f"  [rank {rank}] {label} upgrade reached {new_h}p")
        return height >= min_height

    try:
        done = _try_upgrade([], "Default client")
        if not done:
            for client in quality_upgrade_clients():
                if _try_upgrade(
                    ["--extractor-args", f"youtube:player_client={client}"],
                    f"'{client}' client",
                ):
                    done = True
                    break
            if not done:
                _try_upgrade(cookie_args(), "Cookies")

        if height > original_height:
            _log(
                f"  [rank {rank}] Upgraded raw clip "
                f"{original_height}p -> {height}p"
            )
        else:
            _log(
                f"  [rank {rank}] Keeping {height}p raw "
                f"(no taller source reached {min_height}p)"
            )
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return raw_clip


def _parse_time_to_seconds(t):
    """
    `HH:MM:SS`/`MM:SS`/plain-seconds -> float seconds. download_song's
    start/end come in either shape - the CSV default ("00:01:00") or
    heatmap.pick_clip's already-numeric seconds - and the leading-silence
    correction below needs to do arithmetic on them regardless of which one
    a given caller passed in.
    """
    if ":" in t:
        secs = 0.0
        for part in t.split(":"):
            secs = secs * 60 + float(part)
        return secs
    return float(t)


_LEADING_SILENCE_NOISE_DB = "-50dB"
_LEADING_SILENCE_MIN_DUR = 0.3


def _leading_silence_duration(path, probe_window=8.0):
    """
    Seconds of true silence at the very start of `path`'s audio, or 0.0 if
    none is detected (including on probe failure). Exists because heatmap.
    pick_clip starts a clip 1 second before the video's "most replayed"
    spike - usually the chorus/drop - and plenty of songs have a real
    production pause right before that downbeat, so the clip's own audio
    sometimes opens on dead air instead of content. That's inaudible on its
    own clip but lands right where the crossfade into it happens, so the
    transition appears to fade into silence and then hard-cut into the song
    a second or two later. See download_song's use of this.
    """
    # silencedetect logs its findings at ffmpeg's *info* level, not error -
    # "-v error" (used everywhere else in this file) would silently discard
    # the very lines this function parses, so this call needs its own,
    # more permissive verbosity instead.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-t", str(probe_window), "-i", path,
         "-af", f"silencedetect=noise={_LEADING_SILENCE_NOISE_DB}:d={_LEADING_SILENCE_MIN_DUR}",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for line in result.stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            if float(m.group(1)) > 0.05:
                return 0.0  # silence starts mid-clip, not at the head - nothing to correct
            continue
        m = re.search(r"silence_end:\s*[\d.]+\s*\|\s*silence_duration:\s*([\d.]+)", line)
        if m:
            return float(m.group(1))
    return 0.0


def _trailing_silence_duration(path, duration):
    """
    Seconds of true silence at the very END of `path`'s audio (running
    through to EOF), or 0.0 if none is detected. The tail-side mirror of
    _leading_silence_duration: a downloaded window's audio can just as
    easily run dry a moment before the clip itself ends - not every
    picked window's last second has real content either - and that spot
    is exactly what encode_song's own wipe-off window and an
    OUTGOING transition draw from. Undetected, a crossfade out of this
    clip fades into real silence instead of the song.

    Unlike the leading check (which only needs a short window at the
    head), this scans the whole clip and looks at the LAST silence region
    found, whichever of two shapes it comes in: still open when the file
    ends (no matching "silence_end" at all - silencedetect never emits a
    synthetic one at EOF), or one ffmpeg closes a handful of milliseconds
    before EOF (seen in practice - the detector's own windowing finds a
    sliver of "signal" a few ms before the true end that isn't real
    content resuming). Both count as trailing dead air for this purpose.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", f"silencedetect=noise={_LEADING_SILENCE_NOISE_DB}:d={_LEADING_SILENCE_MIN_DUR}",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    pending_start = None
    last_region = None  # (start, end_or_None) of the most recent silence run
    for line in result.stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            pending_start = float(m.group(1))
            last_region = (pending_start, None)
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and pending_start is not None:
            last_region = (pending_start, float(m.group(1)))
            pending_start = None
    if last_region is None:
        return 0.0
    region_start, region_end = last_region
    if region_end is None or duration - region_end < 0.1:
        return max(0.0, duration - region_start)
    return 0.0


def download_song(rank, url, start="00:01:00", end="00:01:15", min_height=None):
    """
    I/O-bound stage: pull the clip down with yt-dlp into the raw-clip
    cache (RAW_CACHE_DIR), keyed by video ID rather than by rank/title so
    a later run reuses the file outright - see RAW_CACHE_DIR. Overwrites
    any existing cache entry for this video, which is what a caller wants
    when it already decided (see pipeline.py's _download) that this call
    is for an explicit start/end override rather than a cache-eligible
    default clip.

    Downloads land on a staging file, then commit_raw_clip renames them to
    `{video_id}_{height}p.mp4`. After silence-shift corrections,
    ensure_raw_quality may replace a short/low-res file with a taller
    re-fetch of the same window. `min_height` defaults to MIN_RAW_HEIGHT.

    Runs in the download pool - sized larger than the encode pool since
    it's network-bound, not CPU/GPU-bound.
    """
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    video_id = extract_video_id(url)
    staging = raw_staging_path(video_id)

    def _attempt(extra_args, window_start=start, window_end=end, out_path=None):
        dest = out_path or staging
        seed = staging if dest == staging else None
        return _download_section(
            url, dest, window_start, window_end, extra_args, seed_duration_for=seed,
        )

    def _try_silence_shift(window_start, window_end, label):
        """
        Re-download a silence-corrected window into a temp file, and only
        replace staging on success. Writing the shift onto the canonical
        path in place used to destroy the good original when the shifted
        attempt failed - download_song then returned the path anyway
        ("keeping original clip") while the file was gone, and encode later
        failed with "No such file or directory". Rank 16 FAKE LOVE on a
        live chart run hit exactly that.
        """
        tmp_path = f"{RAW_CACHE_DIR}/{video_id}.shift.tmp.mp4"
        try:
            shifted = _attempt(
                [], window_start=window_start, window_end=window_end, out_path=tmp_path,
            )
            if shifted.returncode != 0:
                _log(f"  [rank {rank}] {label} failed, keeping original clip")
                return False
            os.replace(tmp_path, staging)
            duration = _probe_duration(staging)
            if duration is not None:
                with _duration_cache_lock:
                    _duration_cache[staging] = duration
            return True
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    _log(f"  [rank {rank}] Downloading clip...")
    result = _attempt([])
    tried = "Default client"
    for client in download_fallback_clients():
        if result.returncode == 0:
            break
        _log(f"  [rank {rank}] {tried} failed, retrying with '{client}' client...")
        result = _attempt(["--extractor-args", f"youtube:player_client={client}"])
        tried = f"'{client}' client"
    if result.returncode != 0:
        _log(f"  [rank {rank}] {tried} failed, retrying with cookies...")
        result = _attempt(cookie_args())
    if result.returncode != 0:
        raise RuntimeError(f"DOWNLOAD failed (yt-dlp exit {result.returncode}): {result.stderr[-500:]}")

    # Two corrective passes, leading then trailing (see _leading_silence_
    # duration / _trailing_silence_duration). Each is capped at half the
    # window so a clip that's genuinely mostly silent (a broken download,
    # not just a pre-drop pause or a quiet outro) doesn't get shifted
    # arbitrarily far from the section heatmap.pick_clip actually chose.
    # Composed on the same effective_start/effective_end rather than each
    # computed independently off the original start/end, so a clip
    # needing both corrections ends up shifted by their sum, not just
    # whichever ran second.
    effective_start = _parse_time_to_seconds(start)
    effective_end = _parse_time_to_seconds(end)
    window_span = effective_end - effective_start

    silence = _leading_silence_duration(staging)
    if 0 < silence < window_span * 0.5:
        _log(f"  [rank {rank}] {silence:.2f}s of leading silence - re-downloading shifted forward...")
        if _try_silence_shift(
            f"{effective_start + silence:.2f}", f"{effective_end + silence:.2f}",
            "shifted re-download",
        ):
            effective_start += silence
            effective_end += silence

    duration = _cached_duration(staging)
    trailing_silence = _trailing_silence_duration(staging, duration) if duration else 0.0
    if 0 < trailing_silence < window_span * 0.5:
        _log(f"  [rank {rank}] {trailing_silence:.2f}s of trailing silence - re-downloading shifted backward...")
        if _try_silence_shift(
            f"{effective_start - trailing_silence:.2f}",
            f"{effective_end - trailing_silence:.2f}",
            "shifted re-download",
        ):
            effective_start -= trailing_silence
            effective_end -= trailing_silence

    raw_clip = commit_raw_clip(staging, video_id)
    return ensure_raw_quality(
        rank, url,
        f"{effective_start:.2f}", f"{effective_end:.2f}",
        raw_clip, min_height=min_height,
    )


def encode_song(style, raw_clip, rank, title, artist, peak, entry_type,
                views, release_date, months_on_chart, views_gained=None, rank_change="",
                codec=None, head_trim=0.0, tail_trim=0.0, clips_dir=CLIPS_DIR):
    """
    CPU/GPU-bound stage: builds this clip's own timeline -

        [bare (edge clips only)] -> overlay wipes in -> static overlay
        -> overlay wipes out -> [bare (edge clips only)]

    - as one ffmpeg video encode, then muxes a single continuous audio
    track from the same raw window. One graph (see build_song_filter_
    complex) keeps the 24->30 retiming continuous across overlay wipes;
    splitting those into separately-encoded phases restarted pulldown at
    exactly the appear/disappear seams and drifted A/V even with continuous
    audio. The wipe-in/out windows are `overlay_duration` seconds each
    (from style's transition config) and live entirely inside this clip's
    own footage; the bare bookend windows only exist for the first/last
    song in the countdown (head_trim/tail_trim == 0), since every other
    clip instead hands that space to encode_transition to cross-fade into
    its neighbor - see run_pipeline for how head_trim/tail_trim are chosen.

    Runs in the encode pool - sized to match actual hardware/software encode
    capacity, independently of how many downloads are in flight. Leaves
    raw_clip on disk - the transition-building stage still needs the
    clip's true (untrimmed) edges, and it stays there permanently after
    that too, since it lives in the raw-clip cache (see RAW_CACHE_DIR).

    `clips_dir` is where this run's own output (segments, the overlay PNG)
    lands - a per-chart subfolder chosen by the caller, kept separate from
    RAW_CACHE_DIR's global by-video-ID cache and from every other chart's
    output.
    """
    slug        = safe_filename(title)
    final_clip  = f"{clips_dir}/seg_{slug}_rank{rank}.mp4"
    overlay_png = f"{clips_dir}/overlay_{slug}_rank{rank}.png"

    _log(f"  [rank {rank}] Rendering overlay...")
    img = build_overlay_image(
        style,
        rank=rank, title=title, artist=artist,
        peak=peak, release_date=release_date, months_on_chart=months_on_chart,
        views=views, entry_type=entry_type,
        views_gained=views_gained,
        rank_change=rank_change,
    )
    img.save(overlay_png)

    cw  = style.get("canvas", {}).get("width", 1920)
    ch  = style.get("canvas", {}).get("height", 1080)
    fps = style.get("canvas", {}).get("fps", 30)
    transition_cfg      = style.get("transition", {})
    overlay_enter       = transition_cfg.get("overlay_type", "wiperight")
    overlay_exit        = transition_cfg.get("overlay_exit_type", "wipeleft")
    overlay_duration    = transition_cfg.get("overlay_duration", 0.5)
    # The bare bookend windows (edge clips only) stand in for what an actual
    # transition segment would otherwise supply, so they're sized to match
    # that segment's duration, not the overlay wipe's own (usually shorter)
    # duration - otherwise an edge clip's intro/outro would run a different
    # length than the crossfade every interior clip gets instead.
    boundary_duration   = transition_cfg.get("duration", 1.0)

    full_dur = _cached_duration(raw_clip)
    if full_dur is None:
        raise RuntimeError(f"OVERLAY failed: rank {rank} raw clip has no readable duration (corrupt/empty download).")
    window_start = head_trim
    window_end   = full_dur - tail_trim
    window_dur   = window_end - window_start
    lead_bare    = boundary_duration if head_trim == 0 else 0.0
    trail_bare   = boundary_duration if tail_trim == 0 else 0.0
    static_start = window_start + lead_bare + overlay_duration
    static_end   = window_end - trail_bare - overlay_duration
    if static_end <= static_start:
        raise RuntimeError(
            f"OVERLAY failed: clip for rank {rank} ({full_dur:.2f}s) is too short "
            f"to fit a {overlay_duration}s overlay transition on each side."
        )

    video_only = f"{clips_dir}/seg_{slug}_rank{rank}_video.mp4"
    try:
        _run_ffmpeg(
            _build_song_video_cmd, codec,
            (raw_clip, overlay_png, cw, ch, fps, window_start, window_dur,
             overlay_enter, overlay_exit, overlay_duration, lead_bare, trail_bare,
             video_only),
            f"OVERLAY[rank {rank}]",
        )

        # Mux audio to the video's *actual* duration (after CFR retiming),
        # not the theoretical window length. A sub-1% atempo closes any
        # leftover gap without inserting silence - same approach
        # concatenate_clips uses at song boundaries.
        video_dur = _probe_video_duration(video_only) or window_dur
        _run_ffmpeg(
            _build_mux_audio_cmd, codec,
            (video_only, raw_clip, window_start, window_dur, video_dur, final_clip),
            f"MUX[rank {rank}]",
        )
    finally:
        if os.path.exists(video_only):
            os.remove(video_only)
        if os.path.exists(overlay_png):
            os.remove(overlay_png)

    _log(f"  [rank {rank}] Done -> {final_clip}")
    return final_clip


def _build_transition_cmd(codec, clip_a, clip_b, cw, ch, fps, video_transition, duration, out_path):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    dur_a = _cached_duration(clip_a)
    return [
        "ffmpeg",
        "-ss", str(max(dur_a - duration, 0)), "-i", clip_a,
        "-t", str(duration), "-i", clip_b,
        "-filter_complex", build_transition_filter_complex(cw, ch, video_transition, duration, fps),
        "-map", "[vout]", "-map", "[aout]",
        *video_args,
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
        "-t", str(duration),
        "-y", out_path,
    ]


def encode_transition(clip_a, clip_b, cw, ch, fps, out_path, codec=None, video_transition="fade", duration=1.0):
    """
    Builds the short (~`duration`-second) boundary segment stitched between
    two adjacent clips' bare footage instead of re-encoding the whole
    compilation for a single crossfade - small enough to run once per
    boundary, in parallel, alongside the rest of the encode work. Each
    clip's own overlay wipe-in/fade-out already happened inside encode_song,
    so there's no overlay involved here at all (see build_transition_
    filter_complex).
    """
    _run_ffmpeg(_build_transition_cmd, codec, (clip_a, clip_b, cw, ch, fps, video_transition, duration, out_path),
                f"TRANSITION[{os.path.basename(out_path)}]")
    return out_path


def _build_mux_audio_cmd(codec, video_path, raw_clip, audio_start, source_audio_duration,
                         video_duration, out_path):
    # video_path already carries this song's final one-pass video (see
    # encode_song) - stream-copied here, not re-encoded again. atempo
    # stretches the planned source window to the video's actual duration
    # after CFR retiming (usually a few dozen ms). -shortest is a backstop
    # if AAC frame sizing still leaves a hair of slack.
    tempo = 1.0
    if video_duration and source_audio_duration:
        tempo = max(0.5, min(2.0, source_audio_duration / video_duration))
    return [
        "ffmpeg",
        "-i", video_path,
        "-ss", str(audio_start), "-t", str(source_audio_duration), "-i", raw_clip,
        "-filter_complex", f"[1:a]atempo={tempo:.6f},asetpts=PTS-STARTPTS[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
        "-shortest",
        "-y", out_path,
    ]


def _probe_video_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _write_concat_filter_script(path, clip_paths, cw, ch, fps, audio=True):
    """
    Writes a concat-FILTER graph (not the concat demuxer) joining every
    clip in `clip_paths`, to a script file rather than inline on the
    ffmpeg command line - see concatenate_clips for why the filter is
    used at all, and _build_concat_filter_cmd for why this is a file
    (`-filter_complex_script`) instead of a `-filter_complex` argument.

    `audio=False` omits the audio branch entirely (video-only joins).

    When audio is kept (every current caller), each piece's audio branch
    gets a tiny atempo correction to exactly fill that piece's own video
    duration before the concat node. No transition command upstream can
    be trusted to land video and audio on the exact same frame/sample
    boundary - AAC's 1024-sample frame size doesn't divide evenly into an
    arbitrary cut point, so a piece's audio routinely comes out a few
    dozen milliseconds off its own video, even after those commands cap
    both streams to their intended duration. Tried padding the gap with
    actual silence (atrim+apad) first: it does make the two streams equal
    length, but silence is still silence - it just relocates the audible
    dropout to a predictable spot (the end of whichever piece fell short)
    instead of removing it. A sub-1%, inaudible speed adjustment closes
    the same gap without ever inserting dead air.
    """
    parts = []
    for i, clip_path in enumerate(clip_paths):
        parts.append(
            f"[{i}:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps},setsar=1[v{i}]"
        )
        if audio:
            video_duration = _probe_video_duration(clip_path)
            audio_duration = _probe_audio_duration(clip_path)
            tempo = 1.0
            if video_duration and audio_duration:
                tempo = max(0.5, min(2.0, audio_duration / video_duration))
            parts.append(f"[{i}:a]atempo={tempo:.6f},asetpts=PTS-STARTPTS[a{i}]")
    n = len(clip_paths)
    if audio:
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[vout][aout]")
    else:
        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vout]")
    with open(path, "w", encoding="utf-8") as f:
        f.write(";\n".join(parts))


def _build_concat_filter_cmd(codec, clip_paths, filter_script, fps, output_path, audio=True):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    inputs = []
    for path in clip_paths:
        inputs += ["-i", path]
    maps = ["-map", "[vout]"]
    audio_out_args = ["-an"]
    if audio:
        maps += ["-map", "[aout]"]
        audio_out_args = ["-c:a", "aac", "-ar", "48000", "-b:a", "128k"]
    return [
        "ffmpeg", *inputs,
        "-filter_complex_script", filter_script,
        *maps,
        *video_args,
        "-r", str(fps), "-fps_mode", "cfr",
        *audio_out_args,
        "-y", output_path,
    ]


def concatenate_clips(clip_paths, output_path="final_compilation.mp4", codec=None, cw=None, ch=None, fps=None, audio=True):
    """
    Assembles the whole compilation from every song's segment + the
    transitions between them (run_pipeline). Safe to call concurrently -
    the concat list/filter script name is derived from output_path rather
    than shared, and logging goes through the thread-safe _log().

    By default the video track is a plain stream copy (no re-encode) and
    only audio is re-encoded - see below. Pass cw/ch/fps to fully re-encode
    the video too instead, which every current caller does: every clip_path
    here came out of its own independent ffmpeg process, and each one
    writes its own fresh SPS/PPS at the start of its bitstream even with
    identical settings - stream-copying them together leaves those
    parameter-set changes embedded mid-stream. Most software decoders
    shrug that off, but it visibly breaks playback (freeze/black frames
    right at the seam) in players leaning on hardware decode - confirmed in
    VLC, Discord's embedded player, and mobile players. A stream copy is
    only safe here when the caller can guarantee every input segment came
    from the exact same encode session.

    Audio is always re-encoded (not copied): each piece's AAC audio was
    encoded independently and its duration is rarely an exact multiple of
    AAC's 1024-sample frame size, so naively stitching the encoded
    bitstreams together drifts by a fraction of a frame at every boundary,
    producing non-monotonic DTS. Re-encoding audio here is cheap (audio
    decode/encode is trivial next to video) and gives a continuous,
    monotonically increasing timeline.

    Re-encoding alone isn't enough, though - when cw/ch/fps are given
    (every current caller), this uses ffmpeg's concat FILTER, not the
    concat demuxer, specifically for the audio: the demuxer joins each
    piece's independently-encoded AAC bitstream at the packet level, and
    every one of those bitstreams carries its own encoder priming/padding
    samples (from being encoded standalone) that the demuxer never strips
    - measured as an actual ~20-40ms near-silent dropout landing right on
    the seam in the rendered output, audible as a hiccup at every splice
    even though the "video" side of that same demuxer approach was already
    correct. The concat filter instead fully decodes every piece and
    splices raw frames/samples inside one filter graph, so no per-piece
    encoder artifact survives into the join.
    """
    _log(f"  Concatenating {len(clip_paths)} segment(s) -> {output_path}...")
    if cw is not None:
        filter_script = f"{output_path}.filter.txt"
        _write_concat_filter_script(filter_script, clip_paths, cw, ch, fps, audio=audio)
        try:
            _run_ffmpeg(_build_concat_filter_cmd, codec, (clip_paths, filter_script, fps, output_path, audio),
                        f"CONCAT[{os.path.basename(output_path)}]")
        finally:
            os.remove(filter_script)
        return

    list_file = f"{output_path}.concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for path in clip_paths:
            abs_path = os.path.abspath(path).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")
    try:
        result = subprocess.run([
            "ffmpeg", "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
            "-y", output_path,
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"CONCAT failed (ffmpeg exit {result.returncode}): {result.stderr[-500:]}")
    finally:
        os.remove(list_file)
