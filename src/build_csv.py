"""
Build or append to data/songs.csv from a list of YouTube URLs.

Usage:
  # Interactive — prompts for rank/peak/new-entry per song:
  python src/build_csv.py https://youtu.be/xxx https://youtu.be/yyy

  # Pipe URLs from a text file:
  python src/build_csv.py < data/urls.txt

  # Append to existing CSV instead of overwriting:
  python src/build_csv.py --append https://youtu.be/xxx
"""

import subprocess
import json
import csv
import sys
import os

OUTPUT_FILE = "data/songs.csv"
FIELDNAMES  = ["rank", "title", "artist", "url", "peak", "is_new_entry", "start", "end", "years_on_chart", "last_views"]


def clean_url(url):
    """Strip YouTube playlist/radio parameters — keep only the video ID."""
    if "watch?v=" in url:
        video_id = url.split("watch?v=")[1].split("&")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def fetch_title_artist(url):
    """Use yt-dlp to get the video title and uploader without downloading."""
    result = subprocess.run(
        ["yt-dlp", "--no-playlist", "--print", "%(title)s|||%(uploader)s", url],
        capture_output=True, text=True, check=True,
    )
    raw = result.stdout.strip()
    if "|||" in raw:
        raw_title, artist = raw.split("|||", 1)
    else:
        raw_title, artist = raw, ""

    # Strip common YouTube title patterns like "ARTIST - TITLE M/V"
    # and extract just the song name from parentheses when present
    title = raw_title
    if " - " in raw_title:
        parts = raw_title.split(" - ", 1)
        # Use the part after the dash, strip trailing " M/V", " MV", etc.
        title = parts[1]

    for suffix in [" M/V", " MV", " Official Video", " Official MV", " (Official Video)"]:
        if title.upper().endswith(suffix.upper()):
            title = title[: -len(suffix)].strip()

    # Extract text inside parentheses if present, e.g. "🔞GO🔞" → "GO"
    import re
    paren = re.search(r"\(([^)]+)\)", title)
    if paren:
        title = paren.group(1).strip()

    # Remove any remaining non-ASCII emoji/symbols from the edges
    title = re.sub(r"^[^\w]+|[^\w\s\-']+$", "", title).strip()

    return title, artist.strip()


def prompt_song_details(rank_default, title, artist, url):
    """Interactively confirm/override song details."""
    print(f"\n  Detected: \"{title}\" by {artist}")

    rank   = input(f"  Rank [{rank_default}]: ").strip() or str(rank_default)
    title  = input(f"  Title [{title}]: ").strip()      or title
    artist = input(f"  Artist [{artist}]: ").strip()     or artist
    peak   = input(f"  Peak [{rank}]: ").strip()         or rank
    new_e  = input("  New entry? [y/N]: ").strip().lower()
    start  = input("  Clip start [00:01:00]: ").strip()  or "00:01:00"
    end    = input("  Clip end   [00:01:15]: ").strip()  or "00:01:15"

    return {
        "rank":         rank,
        "title":        title,
        "artist":       artist,
        "url":          url,
        "peak":         peak,
        "is_new_entry": "true" if new_e == "y" else "false",
        "start":        start,
        "end":          end,
        "years_on_chart": "",
        "last_views":     "",
    }


def main():
    args = sys.argv[1:]
    append_mode = "--append" in args
    if append_mode:
        args.remove("--append")

    # Accept URLs from args or stdin
    if args:
        urls = args
    else:
        urls = [line.strip() for line in sys.stdin if line.strip()]

    if not urls:
        print("No URLs provided. Pass them as arguments or pipe via stdin.")
        sys.exit(1)

    os.makedirs("data", exist_ok=True)

    # Determine starting rank (continue from existing CSV if appending)
    next_rank = 1
    if append_mode and os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            if rows:
                next_rank = max(int(r["rank"]) for r in rows) + 1

    rows = []
    for i, url in enumerate(urls):
        url = clean_url(url)
        print(f"\nFetching metadata for URL {i + 1}/{len(urls)}...")
        try:
            title, artist = fetch_title_artist(url)
        except subprocess.CalledProcessError as e:
            print(f"  yt-dlp failed for {url}: {e}")
            title, artist = "Unknown", "Unknown"

        row = prompt_song_details(next_rank + i, title, artist, url)
        rows.append(row)

    mode = "a" if append_mode and os.path.exists(OUTPUT_FILE) else "w"
    write_header = mode == "w" or not os.path.exists(OUTPUT_FILE)

    with open(OUTPUT_FILE, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    action = "Appended" if mode == "a" else "Wrote"
    print(f"\n{action} {len(rows)} song(s) to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
