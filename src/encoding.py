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

from overlay import build_overlay_image, build_filter_complex

CLIPS_DIR = "assets/clips"

_print_lock = threading.Lock()


def _log(msg):
    """Thread-safe print — avoids interleaved/garbled lines when songs process concurrently."""
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
    guarantee the hardware actually works at runtime — encode_song()
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


def _build_encode_cmd(codec, raw_clip, overlay_png, cw, ch, final_clip, fps=30):
    video_args = _HW_ENCODE_ARGS.get(codec, _CPU_ENCODE_ARGS)
    return [
        "ffmpeg", "-i", raw_clip, "-i", overlay_png,
        "-filter_complex", build_filter_complex(cw, ch),
        "-map", "[vout]", "-map", "0:a",
        *video_args,
        # Force a constant, uniform frame rate on every clip. Source videos
        # come in at different native rates (e.g. 23.976fps vs 30fps) — the
        # concat demuxer's stream-copy path requires identical timebases
        # across segments, and silently miscomputes frame durations when
        # they differ, producing frozen/slow-motion segments and a wildly
        # wrong total duration despite every frame still being present.
        "-r", str(fps), "-fps_mode", "cfr",
        # Re-encoded (not "-c:a copy"): clips come from different source
        # videos, each with its own original audio timestamp base. Stream
        # copy carries that mismatch through and the concat demuxer can't
        # rebase it cleanly, producing non-monotonic audio DTS at concat
        # time. Re-encoding gives every clip fresh, consistent zero-based
        # audio timestamps, matching the video track's behavior.
        "-c:a", "aac", "-b:a", "128k",
        "-y", final_clip,
    ]


# Explicit fallback if the default client rotation fails outright (e.g. a
# video/session bucketed into YouTube's SABR-only experiment, which blocks
# adaptive-format URLs for most clients). android/mweb still reliably expose
# the legacy progressive format 18 even under that experiment, just capped
# at 360p — build_vf()'s canvas scale/pad step handles the resolution
# mismatch so the clip still concatenates cleanly with 1080p neighbors.
_DOWNLOAD_FALLBACK_CLIENT = "android"


def download_song(rank, title, url, start="00:01:00", end="00:01:15"):
    """
    I/O-bound stage: pull the clip down with yt-dlp. Runs in the download
    pool — sized larger than the encode pool since it's network-bound, not
    CPU/GPU-bound.
    """
    slug     = safe_filename(title)
    raw_clip = f"{CLIPS_DIR}/raw_{slug}_rank{rank}.mp4"

    def _attempt(extra_args):
        return subprocess.run([
            "yt-dlp",
            "--download-sections", f"*{start}-{end}",
            "-S", "res:1080,vcodec:h264,ext:mp4:m4a",
            *extra_args,
            "--no-playlist",
            "-o", raw_clip,
            url,
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")

    _log(f"  [rank {rank}] Downloading clip...")
    result = _attempt([])
    if result.returncode != 0:
        _log(f"  [rank {rank}] Default client failed, retrying with '{_DOWNLOAD_FALLBACK_CLIENT}' client...")
        result = _attempt(["--extractor-args", f"youtube:player_client={_DOWNLOAD_FALLBACK_CLIENT}"])
    if result.returncode != 0:
        raise RuntimeError(f"DOWNLOAD failed (yt-dlp exit {result.returncode}): {result.stderr[-500:]}")

    return raw_clip


def encode_song(style, raw_clip, rank, title, artist, peak, entry_type,
                views, release_date, months_on_chart, views_gained=None, rank_change="", codec=None):
    """
    CPU/GPU-bound stage: burn in the overlay. Runs in the encode pool —
    sized to match actual hardware/software encode capacity, independently
    of how many downloads are in flight.
    """
    slug        = safe_filename(title)
    final_clip  = f"{CLIPS_DIR}/final_{slug}_rank{rank}.mp4"
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

    result = subprocess.run(_build_encode_cmd(codec, raw_clip, overlay_png, cw, ch, final_clip, fps),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 and codec:
        _log(f"  [rank {rank}] GPU encode ({codec}) failed, falling back to CPU...")
        result = subprocess.run(_build_encode_cmd(None, raw_clip, overlay_png, cw, ch, final_clip, fps),
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"OVERLAY failed (ffmpeg exit {result.returncode}): {result.stderr[-500:]}")

    os.remove(raw_clip)
    os.remove(overlay_png)
    _log(f"  [rank {rank}] Done -> {final_clip}")
    return final_clip


def concatenate_clips(clip_paths, output_path="final_compilation.mp4"):
    list_file = f"{CLIPS_DIR}/_concat_list.txt"
    with open(list_file, "w") as f:
        for path in clip_paths:
            abs_path = os.path.abspath(path).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")

    print("Concatenating all clips...")
    subprocess.run([
        "ffmpeg", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", "-y", output_path,
    ], check=True)
    os.remove(list_file)
    print(f"Compilation saved -> {output_path}")
