import os
import sys
import concurrent.futures

# Windows consoles/pipes default Python's stdout to the legacy locale codepage
# (e.g. cp1252), which can't represent Hangul — printing a Korean song title
# would otherwise crash the whole run with UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from overlay import load_style
from encoding import detect_hw_encoder, download_song, encode_song, concatenate_clips, _log
from chart_state import (
    DATA_FILE, load_history, songs_from_search, pre_fetch_all,
    determine_badges, save_run_state, load_songs, clean_csv_titles,
)

# Use YouTube Data API if key is present, otherwise fall back to yt-dlp.
_YT_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
if _YT_API_KEY:
    from youtube_api import batch_fetch_metadata, search_kpop as _search_fn
    print("[backend] YouTube Data API v3")
else:
    from metadata import batch_fetch_metadata
    from search import search_kpop as _search_fn
    print("[backend] yt-dlp (no YOUTUBE_API_KEY set)")

CLIPS_DIR  = "assets/clips"
STYLE_FILE = "assets/templates/style.json"

_BADGE_LABELS = {
    "new_entry":        "NEW ENTRY",
    "re_entry":         "RE-ENTRY",
    "highest_increase": "HIGHEST INCREASE",
    "highest_jump":     "HIGHEST JUMP",
    "":                 "",
}


def run_pipeline(search=None, limit=None, no_filter=False,
                  download_workers=6, encode_workers=3, clean_titles=False):
    """
    K-pop song ranking video generator. Programmatic entry point — the CLI
    (run.py) parses args and calls this directly.
    """
    if clean_titles:
        clean_csv_titles(DATA_FILE)
        return

    os.makedirs(CLIPS_DIR, exist_ok=True)
    style = load_style(STYLE_FILE)

    if search:
        from title_cleaner import clean_titles as clean_titles_fn
        search_limit = limit or 20
        print(f'Searching YouTube: "{search}" (fetching top {search_limit})...\n')
        results = _search_fn(search, limit=search_limit, filter_mv=not no_filter)
        if not results:
            print("No search results returned. Exiting.")
            return
        history = load_history(DATA_FILE)
        songs   = songs_from_search(results, history)
        print("Cleaning up titles via AI...")
        songs = clean_titles_fn(songs)

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
        if limit:
            songs.sort(key=lambda s: int(s.get("rank") or 9999))
            songs = songs[:limit]
            print(f"  Using top {limit} songs from CSV.\n")

    ranked = pre_fetch_all(songs, batch_fetch_metadata)
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
        peak       = song.get("peak", song["rank"])
        return encode_song(
            style, raw_clip,
            rank=song["rank"], title=song["title"], artist=song["artist"],
            peak=peak, entry_type=entry_type,
            views=meta["views"], release_date=meta["release_date"],
            months_on_chart=meta["months_on_chart"],
            views_gained=song["_views_gained"],
            rank_change=song["_rank_change"],
            codec=codec,
        )

    # Two independently-sized pools connected by a pipeline: as each download
    # finishes it's immediately handed to the encode pool, so encoding for
    # earlier songs overlaps with downloading of later ones instead of the
    # two stages being coupled to one shared worker count.
    completed_by_rank, failed = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=download_workers) as dl_pool, \
         concurrent.futures.ThreadPoolExecutor(max_workers=encode_workers) as enc_pool:

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
