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

CLIPS_DIR = "assets/clips"

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


def _probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def download_song(rank, title, url, start="00:01:00", end="00:01:15"):
    """
    I/O-bound stage: pull the clip down with yt-dlp. Runs in the download
    pool - sized larger than the encode pool since it's network-bound, not
    CPU/GPU-bound.
    """
    slug     = safe_filename(title)
    raw_clip = f"{CLIPS_DIR}/raw_{slug}_rank{rank}.mp4"

    def _attempt(extra_args):
        result = subprocess.run([
            "yt-dlp",
            "--download-sections", f"*{start}-{end}",
            "-S", "res:1080,vcodec:h264,ext:mp4:m4a",
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
        if result.returncode == 0 and _probe_duration(raw_clip) is None:
            result.returncode = 1
            result.stderr += "\n(postcheck) downloaded file has no readable duration - treating as a failed attempt"
        return result

    _log(f"  [rank {rank}] Downloading clip...")
    result = _attempt([])
    if result.returncode != 0:
        _log(f"  [rank {rank}] Default client failed, retrying with '{_DOWNLOAD_FALLBACK_CLIENT}' client...")
        result = _attempt(["--extractor-args", f"youtube:player_client={_DOWNLOAD_FALLBACK_CLIENT}"])
    if result.returncode != 0:
        raise RuntimeError(f"DOWNLOAD failed (yt-dlp exit {result.returncode}): {result.stderr[-500:]}")

    return raw_clip


def encode_song(style, raw_clip, rank, title, artist, peak, entry_type,
                views, release_date, months_on_chart, views_gained=None, rank_change="",
                codec=None, head_trim=0.0, tail_trim=0.0):
    """
    CPU/GPU-bound stage: builds this clip's own timeline —

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
    raw_clip on disk; the transition-building stage still needs the clip's
    true (untrimmed) edges, and the caller cleans it up once every
    transition touching this clip is done.
    """
    slug        = safe_filename(title)
    final_clip  = f"{CLIPS_DIR}/seg_{slug}_rank{rank}.mp4"
    overlay_png = f"{CLIPS_DIR}/overlay_{slug}_rank{rank}.png"

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

    full_dur = _probe_duration(raw_clip)
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
            p = f"{CLIPS_DIR}/phase_{slug}_rank{rank}_lead.mp4"
            _run_ffmpeg(_build_bare_cmd, codec, (raw_clip, cw, ch, fps, window_start, lead_bare, p),
                        f"OVERLAY[rank {rank}]")
            phases.append(p)

        p = f"{CLIPS_DIR}/phase_{slug}_rank{rank}_in.mp4"
        _run_ffmpeg(_build_overlay_phase_cmd, codec,
                    (raw_clip, overlay_png, cw, ch, fps, window_start + lead_bare, overlay_duration,
                     overlay_enter, False, p),
                    f"OVERLAY[rank {rank}]")
        phases.append(p)

        p = f"{CLIPS_DIR}/phase_{slug}_rank{rank}_static.mp4"
        _run_ffmpeg(_build_encode_cmd, codec,
                    (raw_clip, overlay_png, cw, ch, p, fps, static_start, static_end - static_start),
                    f"OVERLAY[rank {rank}]")
        phases.append(p)

        p = f"{CLIPS_DIR}/phase_{slug}_rank{rank}_out.mp4"
        _run_ffmpeg(_build_overlay_phase_cmd, codec,
                    (raw_clip, overlay_png, cw, ch, fps, static_end, overlay_duration, overlay_exit, True, p),
                    f"OVERLAY[rank {rank}]")
        phases.append(p)

        if trail_bare:
            p = f"{CLIPS_DIR}/phase_{slug}_rank{rank}_trail.mp4"
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
    dur_a = _probe_duration(clip_a)
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
    with open(list_file, "w") as f:
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
