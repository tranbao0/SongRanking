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
    build_overlay_image, build_filter_complex, build_bare_filter_complex,
    build_overlay_phase_filter_complex, build_transition_filter_complex,
)
from shared.youtube_api import extract_video_id
from shared.ytdlp_throttle import throttle, cookie_args

CLIPS_DIR = "assets/clips"

# Raw clips are cached here permanently, keyed by video ID rather than by
# chart rank (which shifts run to run) - so a song still on the chart next
# time reuses the exact same clip instead of re-downloading it, and reuses
# the exact same window rather than re-rolling heatmap.pick_clip's random
# choice among hot sections (see pipeline.py's _download). Never evicted:
# a 15s clip is small even at thousands of songs.
RAW_CACHE_DIR = f"{CLIPS_DIR}/raw"

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


def _build_encode_cmd(codec, raw_clip, overlay_png, cw, ch, final_clip, fps, trim_start, trim_duration):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    return [
        "ffmpeg", *_trim_input_args(raw_clip, trim_start, trim_duration), "-i", overlay_png,
        "-filter_complex", build_filter_complex(cw, ch),
        "-map", "[vout]", "-map", "0:a",
        *video_args,
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
        "-y", final_clip,
    ]


def _build_bare_cmd(codec, raw_clip, cw, ch, fps, trim_start, trim_duration, out_path):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    return [
        "ffmpeg", *_trim_input_args(raw_clip, trim_start, trim_duration),
        "-filter_complex", build_bare_filter_complex(cw, ch),
        "-map", "[vout]", "-map", "0:a",
        *video_args,
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
        "-y", out_path,
    ]


def _build_overlay_phase_cmd(codec, raw_clip, overlay_png, cw, ch, fps, trim_start, duration,
                              transition, reverse, out_path):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    return [
        "ffmpeg",
        "-ss", str(trim_start), "-t", str(duration), "-i", raw_clip,
        "-loop", "1", "-t", str(duration), "-i", overlay_png,
        "-filter_complex", build_overlay_phase_filter_complex(cw, ch, transition, duration, fps, reverse),
        "-map", "[vout]", "-map", "0:a",
        *video_args,
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
        "-y", out_path,
    ]


# Explicit fallback if the default client rotation fails outright (e.g. a
# video/session bucketed into YouTube's SABR-only experiment, which blocks
# adaptive-format URLs for most clients). android/mweb still reliably expose
# the legacy progressive format 18 even under that experiment, just capped
# at 360p - build_vf()'s canvas scale/pad step handles the resolution
# mismatch so the clip still concatenates cleanly with 1080p neighbors.
_DOWNLOAD_FALLBACK_CLIENT = "android"

# The cookies tier needs its own client rather than reusing
# _DOWNLOAD_FALLBACK_CLIENT: yt-dlp outright refuses to pair the android
# client with cookies ("Skipping client android since it does not support
# cookies"), so a cookies-tier attempt that doesn't override the client
# falls back to the default adaptive-format rotation - right back into the
# same SABR wall the android tier exists to dodge. mweb accepts cookies and
# still serves the same progressive format 18.
_COOKIES_TIER_CLIENT = "mweb"


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


def cached_clip_path(url: str) -> str:
    """Where `url`'s raw clip lives in the cache, whether or not it exists yet."""
    return f"{RAW_CACHE_DIR}/{extract_video_id(url)}.mp4"


def cached_clip(url: str) -> str | None:
    """
    The cached raw clip for `url` if one's already been downloaded *and*
    still probes as a real clip, else None. A stale entry can be corrupt/
    empty - e.g. a previous run got killed mid-download - and existence
    alone doesn't tell them apart, so this probes the same way download_song
    validates a fresh attempt rather than trusting the file is there.
    """
    path = cached_clip_path(url)
    if not os.path.exists(path):
        return None
    if _cached_duration(path) is None:
        return None
    return path


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


def download_song(rank, url, start="00:01:00", end="00:01:15"):
    """
    I/O-bound stage: pull the clip down with yt-dlp into the raw-clip
    cache (RAW_CACHE_DIR), keyed by video ID rather than by rank/title so
    a later run reuses the file outright - see RAW_CACHE_DIR. Overwrites
    any existing cache entry for this video, which is what a caller wants
    when it already decided (see pipeline.py's _download) that this call
    is for an explicit start/end override rather than a cache-eligible
    default clip.

    Runs in the download pool - sized larger than the encode pool since
    it's network-bound, not CPU/GPU-bound.
    """
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    raw_clip = cached_clip_path(url)

    def _attempt(extra_args, window_start=start, window_end=end):
        throttle()
        result = subprocess.run([
            "yt-dlp",
            "--download-sections", f"*{window_start}-{window_end}",
            "-S", "res:1440,vcodec:h264,ext:mp4:m4a",
            *extra_args,
            "--no-playlist",
            "-o", raw_clip,
            url,
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        # yt-dlp can exit 0 even when the section download itself produced
        # nothing usable - e.g. a video bucketed into a player-client
        # experiment whose adaptive-format URLs reject the byte-range
        # requests --download-sections relies on. ffmpeg then writes an
        # empty file while yt-dlp still reports success, so exit code
        # alone isn't a reliable success signal - the output has to
        # actually probe as a real clip too.
        if result.returncode == 0:
            duration = _probe_duration(raw_clip)
            if duration is None:
                result.returncode = 1
                result.stderr += "\n(postcheck) downloaded file has no readable duration - treating as a failed attempt"
            else:
                # The clip is final from here on, so hand this probe's
                # result to everything downstream that would re-spawn
                # ffprobe for the same answer.
                with _duration_cache_lock:
                    _duration_cache[raw_clip] = duration
        return result

    _log(f"  [rank {rank}] Downloading clip...")
    result = _attempt([])
    if result.returncode != 0:
        _log(f"  [rank {rank}] Default client failed, retrying with '{_DOWNLOAD_FALLBACK_CLIENT}' client...")
        result = _attempt(["--extractor-args", f"youtube:player_client={_DOWNLOAD_FALLBACK_CLIENT}"])
    if result.returncode != 0:
        _log(f"  [rank {rank}] '{_DOWNLOAD_FALLBACK_CLIENT}' client failed, retrying with cookies...")
        result = _attempt(["--extractor-args", f"youtube:player_client={_COOKIES_TIER_CLIENT}", *cookie_args()])
    if result.returncode != 0:
        raise RuntimeError(f"DOWNLOAD failed (yt-dlp exit {result.returncode}): {result.stderr[-500:]}")

    # One corrective pass for leading silence (see _leading_silence_duration).
    # Capped at half the window so a clip that's genuinely mostly silent
    # (a broken download, not just a pre-drop pause) doesn't get shifted
    # arbitrarily far from the section heatmap.pick_clip actually chose.
    window_span = _parse_time_to_seconds(end) - _parse_time_to_seconds(start)
    silence = _leading_silence_duration(raw_clip)
    if 0 < silence < window_span * 0.5:
        shifted_start = _parse_time_to_seconds(start) + silence
        shifted_end = _parse_time_to_seconds(end) + silence
        _log(f"  [rank {rank}] {silence:.2f}s of leading silence - re-downloading shifted forward...")
        # --force-overwrites: this attempt targets the same raw_clip path the
        # already-successful first attempt just wrote, so without it yt-dlp
        # sees the destination file already exists and silently skips the
        # download instead of replacing it with the shifted window.
        shifted = _attempt(["--force-overwrites"], window_start=f"{shifted_start:.2f}", window_end=f"{shifted_end:.2f}")
        if shifted.returncode == 0:
            result = shifted
        else:
            _log(f"  [rank {rank}] shifted re-download failed, keeping original clip")

    return raw_clip


def encode_song(style, raw_clip, rank, title, artist, peak, entry_type,
                views, release_date, months_on_chart, views_gained=None, rank_change="",
                codec=None, head_trim=0.0, tail_trim=0.0, clips_dir=CLIPS_DIR):
    """
    CPU/GPU-bound stage: builds this clip's own timeline -

        [bare (edge clips only)] -> overlay wipes in -> static overlay
        -> overlay wipes out -> [bare (edge clips only)]

    - as separate small ffmpeg encodes, then stitches them into one file.
    The wipe-in/out windows are `duration` seconds each (from style's
    transition config) and live entirely inside this clip's own footage;
    the bare bookend windows only exist for the first/last song in the
    countdown (head_trim/tail_trim == 0), since every other clip instead
    hands that space to build_transition_segment() to cross-fade into its
    neighbor - see run_pipeline for how head_trim/tail_trim are chosen.

    Runs in the encode pool - sized to match actual hardware/software encode
    capacity, independently of how many downloads are in flight. Leaves
    raw_clip on disk - the transition-building stage still needs the
    clip's true (untrimmed) edges, and it stays there permanently after
    that too, since it lives in the raw-clip cache (see RAW_CACHE_DIR).

    `clips_dir` is where this run's own output (segments, phases, the
    overlay PNG) lands - a per-chart subfolder chosen by the caller, kept
    separate from RAW_CACHE_DIR's global by-video-ID cache and from every
    other chart's output.
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
    # The bare bookend phases (edge clips only) stand in for what an actual
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
    lead_bare    = boundary_duration if head_trim == 0 else 0.0
    trail_bare   = boundary_duration if tail_trim == 0 else 0.0
    static_start = window_start + lead_bare + overlay_duration
    static_end   = window_end - trail_bare - overlay_duration
    if static_end <= static_start:
        raise RuntimeError(
            f"OVERLAY failed: clip for rank {rank} ({full_dur:.2f}s) is too short "
            f"to fit a {overlay_duration}s overlay transition on each side."
        )

    phases = []
    try:
        if lead_bare:
            p = f"{clips_dir}/phase_{slug}_rank{rank}_lead.mp4"
            _run_ffmpeg(_build_bare_cmd, codec, (raw_clip, cw, ch, fps, window_start, lead_bare, p),
                        f"OVERLAY[rank {rank}]")
            phases.append(p)

        p = f"{clips_dir}/phase_{slug}_rank{rank}_in.mp4"
        _run_ffmpeg(_build_overlay_phase_cmd, codec,
                    (raw_clip, overlay_png, cw, ch, fps, window_start + lead_bare, overlay_duration,
                     overlay_enter, False, p),
                    f"OVERLAY[rank {rank}]")
        phases.append(p)

        p = f"{clips_dir}/phase_{slug}_rank{rank}_static.mp4"
        _run_ffmpeg(_build_encode_cmd, codec,
                    (raw_clip, overlay_png, cw, ch, p, fps, static_start, static_end - static_start),
                    f"OVERLAY[rank {rank}]")
        phases.append(p)

        p = f"{clips_dir}/phase_{slug}_rank{rank}_out.mp4"
        _run_ffmpeg(_build_overlay_phase_cmd, codec,
                    (raw_clip, overlay_png, cw, ch, fps, static_end, overlay_duration, overlay_exit, True, p),
                    f"OVERLAY[rank {rank}]")
        phases.append(p)

        if trail_bare:
            p = f"{clips_dir}/phase_{slug}_rank{rank}_trail.mp4"
            _run_ffmpeg(_build_bare_cmd, codec,
                        (raw_clip, cw, ch, fps, window_end - trail_bare, trail_bare, p),
                        f"OVERLAY[rank {rank}]")
            phases.append(p)

        # Full re-encode (not stream copy) here: these phases were each
        # encoded by independent ffmpeg processes and, unlike the top-level
        # assembly's much longer segments, are cheap enough that a clean
        # re-encode costs nothing - worth it to avoid stream-copying
        # together clips whose SPS/PPS may not agree seam-for-seam (e.g. if
        # one phase silently fell back to CPU encoding and its neighbor
        # didn't), which otherwise shows up as glitching right at the seam.
        concatenate_clips(phases, output_path=final_clip, codec=codec, cw=cw, ch=ch, fps=fps)
    finally:
        for p in phases:
            if os.path.exists(p):
                os.remove(p)
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


def _build_concat_reencode_cmd(codec, list_file, cw, ch, fps, output_path):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    return [
        "ffmpeg", "-f", "concat", "-safe", "0", "-i", list_file,
        "-vf", f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black",
        *video_args,
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
        "-y", output_path,
    ]


def concatenate_clips(clip_paths, output_path="final_compilation.mp4", codec=None, cw=None, ch=None, fps=None):
    """
    Used both to assemble one song's own phases (encode_song) and to
    assemble the whole compilation from every song's segment + the
    transitions between them (run_pipeline), so it has to be safe to call
    concurrently - the concat list file name is derived from output_path
    rather than shared, and logging goes through the thread-safe _log().

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
    AAC's 1024-sample frame size, so the concat demuxer's naive
    cumulative-offset stitching drifts by a fraction of a frame at every
    boundary, producing non-monotonic DTS. ffmpeg tolerates that when
    decoding, but stricter players can drop the audio track outright.
    Re-encoding audio here is cheap (audio decode/encode is trivial next to
    video) and gives a continuous, monotonically increasing timeline.
    """
    list_file = f"{output_path}.concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for path in clip_paths:
            abs_path = os.path.abspath(path).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")

    _log(f"  Concatenating {len(clip_paths)} segment(s) -> {output_path}...")
    try:
        if cw is not None:
            _run_ffmpeg(_build_concat_reencode_cmd, codec, (list_file, cw, ch, fps, output_path),
                        f"CONCAT[{os.path.basename(output_path)}]")
        else:
            result = subprocess.run([
                "ffmpeg", "-f", "concat", "-safe", "0", "-i", list_file,
                "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
                "-y", output_path,
            ], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                raise RuntimeError(f"CONCAT failed (ffmpeg exit {result.returncode}): {result.stderr[-500:]}")
    finally:
        os.remove(list_file)
