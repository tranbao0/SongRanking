"""
Chart state: reading/writing songs.csv, merging in prior-run history
(views/rank deltas), and assigning rank-change badges. Split out of
pipeline.py so the CSV schema and ranking rules live in one place separate
from the download/encode orchestration.
"""

import csv
import os

DATA_FILE = "data/songs.csv"
FIELDNAMES = [
    "rank", "title", "artist", "url", "peak",
    "is_new_entry", "start", "end", "years_on_chart", "release_date",
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
            "release_date":   r["release_date"],
            "last_views":     hist.get("last_views", ""),
            "last_rank":      hist.get("last_rank",  ""),
            "_meta": {
                "views":           r["views"],
                "release_year":    r["release_year"],
                "release_date":    r["release_date"],
                "years_on_chart":  r["years_on_chart"],
                "months_on_chart": r["months_on_chart"],
            },
        })
    return songs


def pre_fetch_all(songs, batch_fetch_metadata):
    """
    Fetch view count + release metadata for every song upfront (skipped when
    search pre-populates _meta), rank by descending view count.
    Returns the list sorted highest-to-lowest views.
    """
    print("Fetching metadata for all songs...\n")

    need_fetch = [song["url"] for song in songs if "_meta" not in song]
    fetched    = batch_fetch_metadata(need_fetch) if need_fetch else {}

    enriched = []
    for song in songs:
        print(f"  {song['title']} by {song['artist']}...")

        if "_meta" in song:
            meta = song["_meta"]
            print(f"    Views: {meta['views']:,}  |  Year: {meta['release_year']}  (from search)")
        else:
            meta = fetched.get(song["url"])
            if meta is None:
                print(f"    WARNING: metadata fetch failed, skipping.")
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
                song["_rank_change"] = "—"
        else:
            song["_rank_change"] = ""

    print()
    return enriched


def determine_badges(songs):
    """
    Assign _entry_type and finalise peak for each song.

    Badge priority (highest wins):
      highest_jump     - biggest positive rank improvement (needs last_rank)
      highest_increase - biggest views_gained (needs last_views); excludes best_jump
      re_entry         - has last_views but no last_rank (dropped off, now back)
      new_entry        - no last_views (never charted before)
      ""               - continuously charted, no special distinction
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

    for col in ("last_views", "last_rank", "release_date"):
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
            "release_date":  song["_meta"]["release_date"],
            "last_views":    "",
            "last_rank":     "",
        }
        row["rank"]         = str(rank_map[url])
        row["title"]        = song["title"]
        row["artist"]       = song["artist"]
        row["last_views"]   = str(views_map[url])
        row["last_rank"]    = str(rank_map[url])
        row["peak"]         = str(peak_map[url])
        row["release_date"] = song["_meta"]["release_date"]
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
    from shared.title_cleaner import clean_titles

    if not os.path.exists(csv_path):
        print(f"{csv_path} does not exist - nothing to clean.")
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
