import subprocess
import csv
import os
import re
import argparse
import threading
import concurrent.futures

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from overlay import load_style, build_overlay_image, build_filter_complex

# Use YouTube Data API if key is present, otherwise fall back to yt-dlp.
_YT_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
if _YT_API_KEY:
    from youtube_api import fetch_metadata, search_kpop as _search_fn
    print("[backend] YouTube Data API v3")
else:
    from metadata import fetch_metadata
    from search import search_kpop as _search_fn
    print("[backend] yt-dlp (no YOUTUBE_API_KEY set)")


def safe_filename(text, max_len=50):
    s = re.sub(r"[^\w]+", "_", text.lower()).strip("_")
    return s[:max_len]


_print_lock = threading.Lock()


def _log(msg):
    """Thread-safe print — avoids interleaved/garbled lines when songs process concurrently."""
    with _print_lock:
        print(msg)


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
            capture_output=True, text=True, check=True,
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


CLIPS_DIR  = "assets/clips"
DATA_FILE  = "data/songs.csv"
STYLE_FILE = "assets/templates/style.json"
FIELDNAMES = [
    "rank", "title", "artist", "url", "peak",
    "is_new_entry", "start", "end", "years_on_chart",
    "last_views", "last_rank",
]


def load_history(csv_path):
    """
    Return a dict keyed by URL containing last_views, last_rank, peak,
    start, and end from the previous run's CSV.
    """
    if not os.path.exists(csv_path):
        return {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        return {
            row["url"]: {k: row.get(k, "") for k in ("last_views", "last_rank", "peak", "start", "end")}
            for row in csv.DictReader(f)
        }


def songs_from_search(results, history):
    """
    Convert search_kpop() results into pipeline song dicts, merging in
    history (last_views, last_rank, peak, clip timestamps) by URL.
    Pre-populates _meta so pre_fetch_all skips redundant yt-dlp calls.
    """
    songs = []
    for r in results:
        hist = history.get(r["url"], {})
        songs.append({
            "title":          r["title"],
            "artist":         r["uploader"],
            "url":            r["url"],
            "rank":           "",
            "peak":           hist.get("peak", ""),
            "is_new_entry":   "false",
            "start":          hist.get("start") or "00:01:00",
            "end":            hist.get("end")   or "00:01:15",
            "years_on_chart": str(r["years_on_chart"]),
            "last_views":     hist.get("last_views", ""),
            "last_rank":      hist.get("last_rank",  ""),
            "_meta": {
                "views":          r["views"],
                "release_year":   r["release_year"],
                "years_on_chart": r["years_on_chart"],
            },
        })
    return songs


def pre_fetch_all(songs):
    """
    Fetch view count + release metadata for every song upfront (skipped when
    search pre-populates _meta), rank by descending view count.
    Returns the list sorted highest-to-lowest views.
    """
    print("Fetching metadata for all songs...\n")
    enriched = []
    for song in songs:
        print(f"  {song['title']} by {song['artist']}...")

        if "_meta" in song:
            meta = song["_meta"]
            print(f"    Views: {meta['views']:,}  |  Year: {meta['release_year']}  (from search)")
        else:
            try:
                meta = fetch_metadata(song["url"])
            except subprocess.CalledProcessError as e:
                print(f"    WARNING: metadata fetch failed ({e}), skipping.")
                continue
            print(f"    Views: {meta['views']:,}  |  Year: {meta['release_year']}")

        raw_last     = song.get("last_views", "").strip()
        last_views   = int(raw_last) if raw_last else None
        views_gained = meta["views"] - last_views if last_views is not None else None

        if views_gained is not None:
            print(f"    +{views_gained:,} views gained")

        enriched.append({**song, "_meta": meta, "_views_gained": views_gained})

    enriched.sort(key=lambda s: s["_meta"]["views"], reverse=True)

    for i, song in enumerate(enriched):
        new_rank     = i + 1
        song["rank"] = str(new_rank)

        raw_last_rank = song.get("last_rank", "").strip()
        if raw_last_rank:
            last_rank = int(raw_last_rank)
            if new_rank < last_rank:
                song["_rank_change"] = "↑"
            elif new_rank > last_rank:
                song["_rank_change"] = "↓"
            else:
                song["_rank_change"] = "−"
        else:
            song["_rank_change"] = ""

    print()
    return enriched


def determine_badges(songs):
    """
    Assign _entry_type and finalise peak for each song.

    Badge priority (highest wins):
      highest_jump     — biggest positive rank improvement (needs last_rank)
      highest_increase — biggest views_gained (needs last_views); excludes best_jump
      re_entry         — has last_views but no last_rank (dropped off, now back)
      new_entry        — no last_views (never charted before)
      ""               — continuously charted, no special distinction
    """
    # Finalise peak: best (lowest-numbered) rank seen across all runs
    for song in songs:
        new_rank      = int(song["rank"])
        hist_peak_str = song.get("peak", "").strip()
        if hist_peak_str:
            song["peak"] = str(min(new_rank, int(hist_peak_str)))
        else:
            song["peak"] = song["rank"]

    def rank_jump(s):
        raw = s.get("last_rank", "").strip()
        return int(raw) - int(s["rank"]) if raw else -1  # positive = climbed

    jumped    = [s for s in songs if rank_jump(s) > 0]
    best_jump = max(jumped, key=rank_jump, default=None)

    gainers = [
        s for s in songs
        if s["_views_gained"] is not None and s["_views_gained"] > 0
        and s is not best_jump
    ]
    best_increase = max(gainers, key=lambda s: s["_views_gained"], default=None)

    for song in songs:
        last_views = song.get("last_views", "").strip()
        last_rank  = song.get("last_rank",  "").strip()

        if song is best_jump:
            song["_entry_type"] = "highest_jump"
        elif song is best_increase:
            song["_entry_type"] = "highest_increase"
        elif last_views and not last_rank:
            song["_entry_type"] = "re_entry"
        elif not last_views:
            song["_entry_type"] = "new_entry"
        else:
            song["_entry_type"] = ""

    return songs


def save_run_state(songs, csv_path):
    """
    Persist current chart state to CSV:
    - Current chart songs: update rank, last_views, last_rank, peak.
    - Songs that fell off the chart: keep last_views, clear last_rank
      so they are detected as re-entries if they return next run.
    - Songs new to the CSV are added as fresh rows.
    """
    current_urls = {s["url"] for s in songs}
    views_map    = {s["url"]: s["_meta"]["views"] for s in songs}
    rank_map     = {s["url"]: s["rank"] for s in songs}
    peak_map     = {s["url"]: s.get("peak", s["rank"]) for s in songs}

    existing_rows = {}
    fieldnames    = list(FIELDNAMES)
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows   = list(reader)
            fieldnames = list(reader.fieldnames or fieldnames)
        existing_rows = {r["url"]: r for r in rows}

    for col in ("last_views", "last_rank"):
        if col not in fieldnames:
            fieldnames.append(col)

    chart_rows = []
    for song in songs:
        url = song["url"]
        row = existing_rows.get(url) or {
            "rank":          song["rank"],
            "title":         song["title"],
            "artist":        song["artist"],
            "url":           url,
            "peak":          song.get("peak", song["rank"]),
            "is_new_entry":  "false",
            "start":         song.get("start", "00:01:00"),
            "end":           song.get("end",   "00:01:15"),
            "years_on_chart": str(song["_meta"]["years_on_chart"]),
            "last_views":    "",
            "last_rank":     "",
        }
        row["rank"]       = str(rank_map[url])
        row["title"]      = song["title"]
        row["artist"]     = song["artist"]
        row["last_views"] = str(views_map[url])
        row["last_rank"]  = str(rank_map[url])
        row["peak"]       = str(peak_map[url])
        chart_rows.append(row)

    chart_rows.sort(key=lambda r: int(r.get("rank") or 999))

    # Songs from previous runs no longer on chart: keep last_views, clear last_rank
    offchart_rows = []
    for url, row in existing_rows.items():
        if url not in current_urls:
            row["last_rank"] = ""
            offchart_rows.append(row)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(chart_rows + offchart_rows)


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
        ], capture_output=True, text=True)

    _log(f"  [rank {rank}] Downloading clip...")
    result = _attempt([])
    if result.returncode != 0:
        _log(f"  [rank {rank}] Default client failed, retrying with '{_DOWNLOAD_FALLBACK_CLIENT}' client...")
        result = _attempt(["--extractor-args", f"youtube:player_client={_DOWNLOAD_FALLBACK_CLIENT}"])
    if result.returncode != 0:
        raise RuntimeError(f"DOWNLOAD failed (yt-dlp exit {result.returncode}): {result.stderr[-500:]}")

    return raw_clip


def encode_song(style, raw_clip, rank, title, artist, peak, entry_type,
                views, years_on_chart, views_gained=None, rank_change="", codec=None):
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
        peak=peak, years_on_chart=years_on_chart,
        views=views, entry_type=entry_type,
        views_gained=views_gained,
        rank_change=rank_change,
    )
    img.save(overlay_png)

    cw  = style.get("canvas", {}).get("width", 1920)
    ch  = style.get("canvas", {}).get("height", 1080)
    fps = style.get("canvas", {}).get("fps", 30)

    result = subprocess.run(_build_encode_cmd(codec, raw_clip, overlay_png, cw, ch, final_clip, fps),
                             capture_output=True, text=True)
    if result.returncode != 0 and codec:
        _log(f"  [rank {rank}] GPU encode ({codec}) failed, falling back to CPU...")
        result = subprocess.run(_build_encode_cmd(None, raw_clip, overlay_png, cw, ch, final_clip, fps),
                                 capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"OVERLAY failed (ffmpeg exit {result.returncode}): {result.stderr[-500:]}")

    os.remove(raw_clip)
    os.remove(overlay_png)
    _log(f"  [rank {rank}] Done -> {final_clip}")
    return final_clip


def load_songs(csv_path):
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def clean_csv_titles(csv_path):
    """
    Re-run AI title cleanup against every existing row in the CSV and
    overwrite title/artist in place. --search only cleans rows freshly
    returned by that search (rows merged in from a prior CSV are left
    as-is), so this is the way to retroactively clean everything already
    on the chart without deleting and re-searching.
    """
    from title_cleaner import clean_titles

    if not os.path.exists(csv_path):
        print(f"{csv_path} does not exist — nothing to clean.")
        return

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    print(f"Cleaning titles for {len(rows)} song(s) via AI...")
    clean_titles(rows)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Updated {csv_path} with cleaned titles.")


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


_BADGE_LABELS = {
    "new_entry":        "NEW ENTRY",
    "re_entry":         "RE-ENTRY",
    "highest_increase": "HIGHEST INCREASE",
    "highest_jump":     "HIGHEST JUMP",
    "":                 "",
}


def main():
    parser = argparse.ArgumentParser(description="K-pop song ranking video generator.")
    parser.add_argument(
        "--search", metavar="QUERY",
        help='Search YouTube for songs instead of reading songs.csv, e.g. "kpop songs 2024"',
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max songs to process. In search mode defaults to 20. In CSV mode processes all unless set.",
    )
    parser.add_argument(
        "--no-filter", dest="no_filter", action="store_true",
        help="Skip MV duration/title filtering when using --search",
    )
    parser.add_argument(
        "--download-workers", type=int, default=6,
        help="Concurrent yt-dlp downloads (I/O-bound, default: 6)",
    )
    parser.add_argument(
        "--encode-workers", type=int, default=3,
        help="Concurrent ffmpeg overlay encodes (CPU/GPU-bound, default: 3)",
    )
    parser.add_argument(
        "--clean-titles", action="store_true",
        help="Re-run AI title cleanup on every row already in songs.csv, then exit (no video rendering).",
    )
    args = parser.parse_args()

    if args.clean_titles:
        clean_csv_titles(DATA_FILE)
        return

    os.makedirs(CLIPS_DIR, exist_ok=True)
    style = load_style(STYLE_FILE)

    if args.search:
        from title_cleaner import clean_titles
        search_limit = args.limit or 20
        print(f'Searching YouTube: "{args.search}" (fetching top {search_limit})...\n')
        results = _search_fn(args.search, limit=search_limit, filter_mv=not args.no_filter)
        if not results:
            print("No search results returned. Exiting.")
            return
        history = load_history(DATA_FILE)
        songs   = songs_from_search(results, history)
        print("Cleaning up titles via AI...")
        songs = clean_titles(songs)

        # Merge in any CSV songs that weren't returned by the search
        if os.path.exists(DATA_FILE):
            csv_songs   = load_songs(DATA_FILE)
            search_urls = {s["url"] for s in songs}
            extra       = [s for s in csv_songs if s["url"] not in search_urls]
            if extra:
                print(f"  Merging {len(extra)} existing CSV song(s) into ranking.\n")
                songs = songs + extra
    else:
        songs = load_songs(DATA_FILE)
        if args.limit:
            songs.sort(key=lambda s: int(s.get("rank") or 9999))
            songs = songs[:args.limit]
            print(f"  Using top {args.limit} songs from CSV.\n")

    ranked = pre_fetch_all(songs)
    determine_badges(ranked)

    print("Rankings by view count:")
    for s in ranked:
        gained     = s["_views_gained"]
        change     = s["_rank_change"]
        badge      = _BADGE_LABELS.get(s["_entry_type"], "")
        gained_str = f"  (+{gained:,} gained)" if gained is not None else ""
        change_str = f" {change}" if change else " (new)"
        badge_str  = f"  [{badge}]" if badge else ""
        print(f"  Rank {s['rank']}{change_str}: {s['title']} — {s['_meta']['views']:,} views{gained_str}{badge_str}")
    print()

    countdown = list(reversed(ranked))

    codec = detect_hw_encoder()
    if codec:
        print(f"[encoder] Hardware acceleration: {codec} (falls back to CPU per-clip on failure)\n")
    else:
        print("[encoder] No GPU encoder detected in this ffmpeg build — using CPU (libx264)\n")

    def _download(song):
        return download_song(
            song["rank"], song["title"], song["url"],
            start=song.get("start", "00:01:00"), end=song.get("end", "00:01:15"),
        )

    def _encode(song, raw_clip):
        meta       = song["_meta"]
        entry_type = song["_entry_type"]
        years      = int(song["years_on_chart"]) if song.get("years_on_chart") else meta["years_on_chart"]
        peak       = song.get("peak", song["rank"])
        return encode_song(
            style, raw_clip,
            rank=song["rank"], title=song["title"], artist=song["artist"],
            peak=peak, entry_type=entry_type,
            views=meta["views"], years_on_chart=years,
            views_gained=song["_views_gained"],
            rank_change=song["_rank_change"],
            codec=codec,
        )

    # Two independently-sized pools connected by a pipeline: as each download
    # finishes it's immediately handed to the encode pool, so encoding for
    # earlier songs overlaps with downloading of later ones instead of the
    # two stages being coupled to one shared worker count.
    completed_by_rank, failed = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.download_workers) as dl_pool, \
         concurrent.futures.ThreadPoolExecutor(max_workers=args.encode_workers) as enc_pool:

        download_futures = {dl_pool.submit(_download, song): song for song in countdown}
        encode_futures = {}

        for future in concurrent.futures.as_completed(download_futures):
            song = download_futures[future]
            try:
                raw_clip = future.result()
            except RuntimeError as e:
                _log(f"  ERROR: Rank {song['rank']} ({song['title']}) — {e}\n")
                failed.append((song["rank"], song["title"]))
                continue
            encode_future = enc_pool.submit(_encode, song, raw_clip)
            encode_futures[encode_future] = song

        for future in concurrent.futures.as_completed(encode_futures):
            song = encode_futures[future]
            try:
                completed_by_rank[song["rank"]] = future.result()
            except RuntimeError as e:
                _log(f"  ERROR: Rank {song['rank']} ({song['title']}) — {e}\n")
                failed.append((song["rank"], song["title"]))

    # Reassemble in countdown order (highest rank number -> #1) regardless of completion order.
    completed = [completed_by_rank[s["rank"]] for s in countdown if s["rank"] in completed_by_rank]

    if completed:
        concatenate_clips(completed)

    save_run_state(ranked, DATA_FILE)
    print("Updated CSV for next run.\n")

    if failed:
        print("\nFailed songs:")
        for rank, title in failed:
            print(f"  Rank {rank}: {title}")


if __name__ == "__main__":
    main()
