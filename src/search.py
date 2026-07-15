"""
Search for K-pop songs via yt-dlp and print ranked results.

Usage:
  python src/search.py "kpop songs" --limit 20 
  python src/search.py "kpop hits" --csv          # emit CSV rows ready for songs.csv

yt-dlp does the heavy lifting: ytsearch<N> + --flat-playlist + --print
fetches metadata without downloading any video.

Swap in youtube_api.py (see src/youtube_api.py) once an API key is available.
"""

import subprocess
import sys
import argparse
from datetime import date


PRINT_TEMPLATE = "%(id)s|||%(title)s|||%(uploader)s|||%(view_count)s|||%(upload_date)s"


def search_kpop(query: str, limit: int = 50) -> list[dict]:
    """
    Run yt-dlp search and return a list of result dicts.
    Fields: id, title, uploader, views, upload_date, url, years_on_chart
    Results are sorted by view count descending.
    """
    result = subprocess.run(
        [
            "yt-dlp",
            f"ytsearch{limit}:{query}",
            "--flat-playlist",
            "--no-warnings",
            "--print", PRINT_TEMPLATE,
        ],
        capture_output=True, text=True, check=True,
    )

    songs = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|||")
        if len(parts) != 5:
            continue

        vid_id, title, uploader, view_count_raw, upload_date_raw = parts

        try:
            views = int(view_count_raw)
        except ValueError:
            views = 0

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
            "release_year":   release_year,
            "years_on_chart": years_on_chart,
            "url":            f"https://www.youtube.com/watch?v={vid_id.strip()}",
        })

    songs.sort(key=lambda s: s["views"], reverse=True)
    return songs


def print_table(songs: list[dict]) -> None:
    """Print results as a formatted table."""
    if not songs:
        print("No results found.")
        return

    header = f"{'#':<4} {'Views':>14}  {'Title':<45}  {'Uploader':<30}  {'Year'}"
    print(header)
    print("-" * len(header))
    for i, s in enumerate(songs, 1):
        title    = s["title"][:43] + ".." if len(s["title"]) > 45 else s["title"]
        uploader = s["uploader"][:28] + ".." if len(s["uploader"]) > 30 else s["uploader"]
        print(f"{i:<4} {s['views']:>14,}  {title:<45}  {uploader:<30}  {s['release_year']}")


def print_csv(songs: list[dict]) -> None:
    """
    Emit CSV rows compatible with data/songs.csv so they can be piped in directly.
    Ranks are assigned by view count (most views = rank 1).
    """
    print("rank,title,artist,url,peak,is_new_entry,start,end,years_on_chart,last_views")
    for i, s in enumerate(songs, 1):
        title  = s["title"].replace(",", " ")
        artist = s["uploader"].replace(",", " ")
        print(
            f"{i},{title},{artist},{s['url']},{i},false,"
            f"00:01:00,00:01:15,{s['years_on_chart']},"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Search YouTube for K-pop songs via yt-dlp.")
    parser.add_argument("query", help='Search query, e.g. "kpop songs 2024"')
    parser.add_argument("--limit", type=int, default=50,
                        help="Max results to fetch (default: 50)")
    parser.add_argument("--csv", action="store_true",
                        help="Output CSV rows for songs.csv instead of a table")
    args = parser.parse_args()

    print(f"Searching: \"{args.query}\" (up to {args.limit} results)...\n",
          file=sys.stderr)

    songs = search_kpop(args.query, limit=args.limit)

    if args.csv:
        print_csv(songs)
    else:
        print_table(songs)


if __name__ == "__main__":
    main()
