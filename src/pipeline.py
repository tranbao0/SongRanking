import subprocess
import csv
import os
import re

from metadata import fetch_metadata
from overlay import load_style, build_vf


def safe_filename(text):
    """Lowercase, replace any non-alphanumeric run with a single underscore."""
    s = re.sub(r"[^\w]+", "_", text.lower())
    return s.strip("_")

CLIPS_DIR  = "assets/clips"
DATA_FILE  = "data/songs.csv"
STYLE_FILE = "assets/templates/style.json"


def pre_fetch_all(songs):
    """
    Fetch view count + release metadata for every song upfront,
    then assign ranks by descending view count (most views = rank 1).
    Returns the list sorted highest-to-lowest views.
    """
    print("Fetching metadata for all songs...\n")
    enriched = []
    for song in songs:
        print(f"  {song['title']} by {song['artist']}...")
        try:
            meta = fetch_metadata(song["url"])
        except subprocess.CalledProcessError as e:
            print(f"    WARNING: metadata fetch failed ({e}), skipping.")
            continue
        print(f"    Views: {meta['views']:,}  |  Release year: {meta['release_year']}")
        enriched.append({**song, "_meta": meta})

    # Sort descending: rank 1 = most views
    enriched.sort(key=lambda s: s["_meta"]["views"], reverse=True)

    # Assign view-count-based ranks (override CSV rank)
    for i, song in enumerate(enriched):
        song["rank"] = str(i + 1)

    print()
    return enriched


def process_song(style, rank, title, artist, url, peak, is_new_entry,
                 views, years_on_chart, start="00:01:00", end="00:01:15"):
    print(f"--- Processing Rank {rank}: {title} ---")

    slug       = safe_filename(title)
    raw_clip   = f"{CLIPS_DIR}/raw_{slug}_rank{rank}.mp4"
    final_clip = f"{CLIPS_DIR}/final_{slug}_rank{rank}.mp4"

    # ── Download the clip ────────────────────────────────────────────────────
    print("  Downloading clip...")
    subprocess.run([
        "yt-dlp",
        "--external-downloader", "ffmpeg",
        "--external-downloader-args", f"ffmpeg_i:-ss {start} -to {end}",
        "-S", "res:1080,vcodec:h264,ext:mp4:m4a",
        "-o", raw_clip,
        url,
    ], check=True)

    # ── Render overlay ───────────────────────────────────────────────────────
    print("  Rendering overlay...")
    vf = build_vf(
        style,
        rank=rank, title=title, artist=artist,
        peak=peak, years_on_chart=years_on_chart,
        views=views, is_new_entry=is_new_entry,
    )
    subprocess.run([
        "ffmpeg", "-i", raw_clip,
        "-vf", vf, "-c:a", "copy", "-y",
        final_clip,
    ], check=True)

    os.remove(raw_clip)
    print(f"  Done -> {final_clip}\n")
    return final_clip


def load_songs(csv_path):
    """
    Required : rank, title, artist, url, peak
    Optional : is_new_entry, start, end, years_on_chart
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def concatenate_clips(clip_paths, output_path="final_compilation.mp4"):
    """
    Stitch clips together with stream-copy (no re-encode).
    Receives paths in countdown order (rank N … rank 1).
    """
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


def main():
    os.makedirs(CLIPS_DIR, exist_ok=True)
    style = load_style(STYLE_FILE)
    songs = load_songs(DATA_FILE)

    # Pre-fetch metadata and rank by view count
    ranked = pre_fetch_all(songs)

    print("Rankings by view count:")
    for s in ranked:
        print(f"  Rank {s['rank']}: {s['title']} — {s['_meta']['views']:,} views")
    print()

    # Process in countdown order so the final file plays rank N → rank 1
    countdown = list(reversed(ranked))

    completed, failed = [], []
    for song in countdown:
        meta         = song["_meta"]
        is_new_entry = song.get("is_new_entry", "false").strip().lower() == "true"
        years        = int(song["years_on_chart"]) if song.get("years_on_chart") else meta["years_on_chart"]
        start        = song.get("start", "00:01:00")
        end          = song.get("end",   "00:01:15")

        # peak: use CSV value if provided, otherwise same as assigned rank
        peak = song.get("peak", "").strip() or song["rank"]

        try:
            clip = process_song(
                style,
                rank=song["rank"], title=song["title"], artist=song["artist"],
                url=song["url"], peak=peak, is_new_entry=is_new_entry,
                views=meta["views"], years_on_chart=years,
                start=start, end=end,
            )
            completed.append(clip)
        except subprocess.CalledProcessError as e:
            print(f"  ERROR: Rank {song['rank']} ({song['title']}) failed — skipping. ({e})\n")
            failed.append((song["rank"], song["title"]))

    # Concatenate in the order they were processed (countdown N → 1)
    if completed:
        concatenate_clips(completed)

    if failed:
        print("\nFailed songs:")
        for rank, title in failed:
            print(f"  Rank {rank}: {title}")


if __name__ == "__main__":
    main()
