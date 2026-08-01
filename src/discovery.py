"""
Merges channel discovery across providers into the `channels` table.

Wikidata is authoritative: its entries are always kept. kworb is a
popularity-based seed and is unioned in on top, so it only adds channels
Wikidata missed rather than gating what Wikidata already found. A manual
per-genre yaml patches remaining gaps and an exclude yaml prunes false
positives (mainly from kworb's regional charts).
"""

from datetime import date
from pathlib import Path

import yaml

import db
from providers import kworb, wikidata

CHANNELS_DIR = Path(__file__).parent.parent / "data" / "channels"

KNOWN_GENRES = sorted(set(wikidata.GENRE_QIDS) | set(kworb.GENRE_COUNTRIES))


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


def discover_genre(genre: str) -> list[dict]:
    """
    Cross-reference all sources for one genre and return the merged,
    de-duplicated, exclude-filtered channel list (not yet persisted).
    """
    merged: dict[str, dict] = {}

    for entry in kworb.discover_channels(genre):
        merged[entry["channel_id"]] = entry
    for entry in wikidata.discover_channels(genre):
        merged[entry["channel_id"]] = entry  # wikidata wins on overlap
    for entry in _manual_channels(genre):
        merged[entry["channel_id"]] = entry  # manual overrides everything

    for channel_id in _excluded_channel_ids(genre):
        merged.pop(channel_id, None)

    return list(merged.values())


def sync_channels(genres: list[str]) -> dict[str, int]:
    """
    Run discovery for each genre and upsert results into data/registry.db.
    Returns {genre: channel_count} for reporting.
    """
    conn = db.get_connection()
    counts = {}
    try:
        for genre in genres:
            channels = discover_genre(genre)
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
            counts[genre] = len(channels)
        conn.commit()
    finally:
        conn.close()
    return counts
