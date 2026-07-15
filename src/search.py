"""
Search for K-pop songs via yt-dlp and print ranked results.

Usage:
  python src/search.py "kpop songs" --limit 20
  python src/search.py "kpop hits" --csv          # emit CSV rows ready for songs.csv
  python src/search.py "kpop hits" --no-filter    # skip duration/title filtering

yt-dlp does the heavy lifting: ytsearch<N> + --flat-playlist + --print
fetches metadata without downloading any video.

Swap in youtube_api.py (see src/youtube_api.py) once an API key is available.
"""

import subprocess
import sys
import re
import argparse
from datetime import date


PRINT_TEMPLATE = (
    "%(id)s|||%(title)s|||%(uploader)s"
    "|||%(view_count)s|||%(upload_date)s|||%(duration)s"
)

# Titles matching this pattern are almost certainly compilations, playlists,
# or aggregator videos rather than individual official MVs.
_BLOCKLIST = re.compile(
    r"\b("
    r"compilation|playlist|mixtape|medley"
    r"|top\s*\d+"
    r"|best\s+of"
    r"|all\s+songs?"
    r"|full\s+album"
    r"|greatest\s+hits"
    r"|mash.?up"
    r"|collection"
    r"|ranking"
    r"|mix"           # "kpop mix 2024" — distinct from "remix" (no word boundary match)
    r")\b",
    re.IGNORECASE,
)

# Official MV duration window: 90 s (short singles) → 720 s (extended cuts).
# Anything shorter is a teaser/clip; longer is a live set or compilation.
_MIN_DURATION = 90
_MAX_DURATION = 720


def _is_valid_mv(song: dict) -> bool:
    """Return True if the video is likely an official single/MV."""
    dur = song.get("duration") or 0
    if dur < _MIN_DURATION or dur > _MAX_DURATION:
        return False
    if _BLOCKLIST.search(song["title"]):
        return False
    return True


def search_kpop(query: str, limit: int = 50, filter_mv: bool = True) -> list[dict]:
    """
    Run yt-dlp search and return a list of result dicts.
    Fields: id, title, uploader, views, upload_date, duration, url, years_on_chart

    When filter_mv=True (default), removes compilations, playlists, and
    videos outside the typical MV duration range before returning.
    Results are sorted by view count descending.
    """
    # Fetch more than needed to compensate for filtered-out results.
    fetch_n = min(limit * 3, 200) if filter_mv else limit

    result = subprocess.run(
        [
            "yt-dlp",
            f"ytsearch{fetch_n}:{query}",
            "--flat-playlist",
            "--no-warnings",
            "--print", PRINT_TEMPLATE,
        ],
        capture_output=True, text=True, check=True,
    )

    songs = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|||")
        if len(parts) != 6:
            continue

        vid_id, title, uploader, view_count_raw, upload_date_raw, duration_raw = parts

        try:
            views = int(view_count_raw)
        except ValueError:
            views = 0

        try:
            duration = int(float(duration_raw))
        except (ValueError, TypeError):
            duration = 0

        if len(upload_date_raw) >= 4:
            release_year = int(upload_date_raw[:4])
        else:
            release_year = date.today().year

        years_on_chart = max(1, date.today().year - release_year + 1)

        songs.append({
            "id":             vid_id.strip(),
            "title":          title.strip(),
            "uploader":       uploader.strip(),
            "views":          views,
            "upload_date":    upload_date_raw.strip(),
            "duration":       duration,
            "release_year":   release_year,
            "years_on_chart": years_on_chart,
            "url":            f"https://www.youtube.com/watch?v={vid_id.strip()}",
        })

    if filter_mv:
        before = len(songs)
        songs  = [s for s in songs if _is_valid_mv(s)]
        removed = before - len(songs)
        if removed:
            print(f"  Filtered out {removed} non-MV result(s) "
                  f"(compilations / wrong duration).", file=sys.stderr)

    songs.sort(key=lambda s: s["views"], reverse=True)
    return songs[:limit]


def print_table(songs: list[dict]) -> None:
    """Print results as a formatted table."""
    if not songs:
        print("No results found.")
        return

    header = f"{'#':<4} {'Views':>14}  {'Dur':>5}  {'Title':<45}  {'Uploader':<30}  {'Year'}"
    print(header)
    print("-" * len(header))
    for i, s in enumerate(songs, 1):
        title    = s["title"][:43] + ".." if len(s["title"]) > 45 else s["title"]
        uploader = s["uploader"][:28] + ".." if len(s["uploader"]) > 30 else s["uploader"]
        dur_min  = f"{s['duration'] // 60}:{s['duration'] % 60:02d}" if s["duration"] else "--:--"
        print(f"{i:<4} {s['views']:>14,}  {dur_min:>5}  {title:<45}  {uploader:<30}  {s['release_year']}")


def print_csv(songs: list[dict]) -> None:
    """
    Emit CSV rows compatible with data/songs.csv so they can be piped in directly.
    Ranks are assigned by view count (most views = rank 1).
    """
    print("rank,title,artist,url,peak,is_new_entry,start,end,years_on_chart,last_views,last_rank")
    for i, s in enumerate(songs, 1):
        title  = s["title"].replace(",", " ")
        artist = s["uploader"].replace(",", " ")
        print(
            f"{i},{title},{artist},{s['url']},{i},false,"
            f"00:01:00,00:01:15,{s['years_on_chart']},,"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Search YouTube for K-pop songs via yt-dlp.")
    parser.add_argument("query", help='Search query, e.g. "kpop songs 2024"')
    parser.add_argument("--limit", type=int, default=50,
                        help="Max results to return after filtering (default: 50)")
    parser.add_argument("--csv", action="store_true",
                        help="Output CSV rows for songs.csv instead of a table")
    parser.add_argument("--no-filter", dest="no_filter", action="store_true",
                        help="Skip MV duration/title filtering")
    args = parser.parse_args()

    print(f"Searching: \"{args.query}\" (up to {args.limit} results)...\n",
          file=sys.stderr)

    songs = search_kpop(args.query, limit=args.limit, filter_mv=not args.no_filter)

    if args.csv:
        print_csv(songs)
    else:
        print_table(songs)


if __name__ == "__main__":
    main()
