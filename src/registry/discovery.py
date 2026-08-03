"""
Merges channel discovery across providers into the `channels` table.

Wikidata is the only automated source, and is authoritative: an artist it
tags with the genre and links to a YouTube channel is included. A manual
per-genre yaml adds what it cannot - label, distributor and broadcaster
channels have no Wikidata artist entry however much of the genre they
carry - and an exclude yaml prunes anything that shouldn't be tracked.

A popularity-seeded provider (kworb's per-country YouTube charts) was
tried and removed. Country charts rank what charts *in* a market rather
than what belongs to a genre, so it kept introducing acts from other
genres entirely, and their uploads are labelled impeccably enough by
their own labels that no title-level filter could tell them apart. The
channels it found that were worth keeping are curated in the manual yaml.

A third yaml, `<genre>_manual_videos.yaml`, pins individual videos rather
than whole channels - for a genre song that sits on a channel which is
mostly something else entirely (a movie studio's channel, a late-night
show's channel), where adding the channel via the manual yaml above would
pull in everything else it has ever posted. Each pinned video still needs
a row in `channels` (videos have a NOT NULL FK to it), so one is
synthesized here with source "manual_video" - catalog.py recognizes that
source and fetches only the pinned video ID(s) for it, never that
channel's upload history.
"""

from datetime import date
from pathlib import Path

import requests
import yaml

from registry import db
from registry.providers import wikidata

CHANNELS_DIR = Path(__file__).parent.parent.parent / "data" / "channels"

KNOWN_GENRES = sorted(wikidata.GENRE_QIDS)


def _load_yaml_list(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _manual_channels(genre: str) -> list[dict]:
    entries = _load_yaml_list(CHANNELS_DIR / f"{genre}_manual.yaml")
    return [
        {
            "channel_id":   e["channel_id"],
            "genre":        genre,
            "display_name": e.get("display_name", e["channel_id"]),
            "source":       "manual",
            "source_ref":   None,
            "added_date":   date.today().isoformat(),
        }
        for e in entries
    ]


def _excluded_channel_ids(genre: str) -> set[str]:
    entries = _load_yaml_list(CHANNELS_DIR / f"{genre}_exclude.yaml")
    return {e["channel_id"] if isinstance(e, dict) else e for e in entries}


def manual_videos(genre: str) -> list[dict]:
    """
    Individually pinned videos for `genre`, from `<genre>_manual_videos.yaml`.
    Each entry names the real channel that uploaded it - catalog.py uses
    that to fetch just this video rather than the channel's history.
    """
    entries = _load_yaml_list(CHANNELS_DIR / f"{genre}_manual_videos.yaml")
    return [
        {
            "video_id":     e["video_id"],
            "channel_id":   e["channel_id"],
            "genre":        genre,
            "display_name": e.get("display_name", e["video_id"]),
        }
        for e in entries
    ]


def discover_genre(genre: str) -> list[dict]:
    """
    Cross-reference all sources for one genre and return the merged,
    de-duplicated, exclude-filtered channel list (not yet persisted).
    """
    merged: dict[str, dict] = {}

    # Lowest priority: a placeholder so a pinned video's channel has
    # somewhere to live (see module docstring). setdefault, not
    # assignment - a channel that's also tracked in full below keeps that
    # fuller entry, since its normal walk already reaches the pinned video.
    for video in manual_videos(genre):
        merged.setdefault(video["channel_id"], {
            "channel_id":   video["channel_id"],
            "genre":        genre,
            "display_name": f"{video['display_name']} (pinned video only)",
            "source":       "manual_video",
            "source_ref":   None,
            "added_date":   date.today().isoformat(),
        })

    for entry in wikidata.discover_channels(genre):
        merged[entry["channel_id"]] = entry
    for entry in _manual_channels(genre):
        merged[entry["channel_id"]] = entry  # manual overrides everything

    for channel_id in _excluded_channel_ids(genre):
        merged.pop(channel_id, None)

    return list(merged.values())


def _purge_excluded(conn, genre: str) -> int:
    """
    Remove excluded channels and everything catalogued from them: their
    videos, and any song left holding no videos as a result.

    Deleting the videos matters as much as the channel row. A chart reads
    videos joined to channels, so an excluded channel's uploads would keep
    charting under the genre they were wrongly tagged with until they were
    removed too. Returns the number of channels dropped.
    """
    excluded = _excluded_channel_ids(genre)
    if not excluded:
        return 0

    params = list(excluded)
    placeholders = ",".join("?" for _ in params)
    conn.execute(
        f"""DELETE FROM view_snapshots WHERE video_id IN
            (SELECT video_id FROM videos WHERE channel_id IN ({placeholders}))""",
        params,
    )
    conn.execute(f"DELETE FROM videos WHERE channel_id IN ({placeholders})", params)
    conn.execute(
        "DELETE FROM songs WHERE song_id NOT IN "
        "(SELECT song_id FROM videos WHERE song_id IS NOT NULL)"
    )
    cursor = conn.execute(f"DELETE FROM channels WHERE channel_id IN ({placeholders})", params)
    return cursor.rowcount


def sync_channels(genres: list[str]) -> dict[str, int]:
    """
    Run discovery for each genre and upsert results into data/registry.db.
    Committed per genre rather than once at the end, and one genre's
    Wikidata failure doesn't take the rest down with it - discover_channels
    already retries a transient error itself (see wikidata.py), so a genre
    only lands here if that's exhausted. Returns {genre: channel_count} for
    reporting; a genre whose discovery failed is omitted from it.
    """
    conn = db.get_connection()
    counts = {}
    try:
        for genre in genres:
            print(f"  [discovery] {genre}: querying Wikidata and manual sources...")

            # Excluded channels are dropped from the registry, not merely
            # left out of this run's discovery. Filtering discover_genre()'s
            # output alone would silently do nothing for a channel already
            # catalogued - the upsert below only inserts and updates - so
            # adding an entry to the exclude file would appear to have no
            # effect on exactly the channel it was written for. Independent
            # of the Wikidata query below, so it's committed on its own and
            # applies even when that query ends up failing.
            _purge_excluded(conn, genre)
            conn.commit()

            try:
                channels = discover_genre(genre)
            except requests.RequestException as e:
                print(f"  [discovery] {genre}: Wikidata query failed ({e}), skipping this genre")
                continue

            conn.executemany(
                """
                INSERT INTO channels (channel_id, genre, display_name, source, source_ref, added_date)
                VALUES (:channel_id, :genre, :display_name, :source, :source_ref, :added_date)
                ON CONFLICT(channel_id) DO UPDATE SET
                    genre=excluded.genre,
                    display_name=excluded.display_name,
                    source=excluded.source,
                    source_ref=excluded.source_ref
                """,
                channels,
            )
            conn.commit()
            counts[genre] = len(channels)
    finally:
        conn.close()
    return counts
