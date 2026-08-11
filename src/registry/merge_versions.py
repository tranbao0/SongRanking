"""
One-off migration: merges already-grouped `songs` rows that split a single
underlying song into several rows purely because of a remix/acoustic/
instrumental/vocal/karaoke/a cappella/sped-up/slowed/language-version tag -
the DO-NOT-merge rule song_grouping.py used to enforce for those tags,
reversed 2026-08-10 (see song_grouping's module docstring and
docs/manual-grouping-prompt.md's Rules-section note).

Not something a normal sync or `regroup` picks up on its own: grouping is
additive (see song_grouping's module docstring) and only ever classifies a
video that doesn't have a song_id yet, so two already-song_id'd videos that
now normalize to the same key under the updated normalize_title stay split
forever unless something explicitly reconsiders them. This module is that
one-off reconsideration - run once after the rule change, not part of the
regular sync/regroup/audit cycle.

Follows docs/manual-grouping-prompt.md's own "How to apply a merge"
convention: lowest song_id survives, canonical_title resets to the shortest
title among the group's videos, and any songs row left with no videos is
deleted.

Only catches version pairs whose titles still share enough text to
normalize together under the new rule - overwhelmingly the bracket-tagged
cases ("(Acoustic Ver.)", "(Remix)", "(Japanese Ver.)", "(Sped Up Ver.)",
...) that make up the vast majority of this project's real data (see the
doc's own precedent list). A handful of non-bracket-shaped exceptions (a
producer-collab title like "WORK Pt.2 - ATEEZ X Don Diablo") won't be found
here and are left for a future AI-assisted or manual pass, the same way the
doc already treats similar edge cases as out of scope for a single sweep.
"""

import shutil
from datetime import datetime
from pathlib import Path

from registry import db, song_grouping
from registry.catalog import _SHARED_CHANNEL_VIDEO_THRESHOLD, _confirmed_artists


def _write_backup() -> Path:
    """Copy the local working copy as-is, before this run's own changes."""
    backup = db.LOCAL_WORKING_DB_PATH.parent / f"registry.{datetime.now().strftime('%Y%m%d-%H%M%S')}.pre-merge-versions.db"
    shutil.copy2(db.LOCAL_WORKING_DB_PATH, backup)
    return backup


def _index_by_channel(conn) -> dict:
    """
    {channel_id: artist index to use when matching that channel's songs} -
    the same large-shared-channel test regroup.py applies per channel,
    built once up front here rather than re-derived per song.
    """
    artist_patterns, _ = _confirmed_artists(conn)
    index_by_genre = {
        genre: song_grouping.build_artist_index(patterns) for genre, patterns in artist_patterns.items()
    }
    no_index = song_grouping.build_artist_index([])

    indexes = {}
    for row in conn.execute("SELECT channel_id, genre, source, last_known_video_count FROM channels"):
        is_shared = (
            row["source"] != "wikidata"
            and (row["last_known_video_count"] or 0) > _SHARED_CHANNEL_VIDEO_THRESHOLD
        )
        indexes[row["channel_id"]] = index_by_genre.get(row["genre"], no_index) if is_shared else no_index
    return indexes


def find_clusters(conn, genre: str | None = None) -> list[list[int]]:
    """
    Group existing songs, per anchor channel, by song_grouping's current
    match key (same key tier 1/2 already uses for new uploads - see
    song_grouping._match_key). Returns a list of clusters (each a list of
    2+ song_ids that should become one song); a song already alone under
    its key isn't included. `genre` scopes to songs anchored on a channel
    of that genre, same as decouple/regroup's --genre.
    """
    index_by_channel = _index_by_channel(conn)
    no_index = song_grouping.build_artist_index([])

    query = "SELECT s.song_id, s.channel_id, s.canonical_title FROM songs s"
    params: list = []
    if genre is not None:
        query += " JOIN channels c ON c.channel_id = s.channel_id WHERE c.genre = ?"
        params.append(genre)

    keyed: dict[tuple, list[int]] = {}
    for row in conn.execute(query, params):
        index = index_by_channel.get(row["channel_id"], no_index)
        key = song_grouping._match_key(row["canonical_title"], index)
        if key is None:
            continue
        keyed.setdefault((row["channel_id"], key), []).append(row["song_id"])

    return [song_ids for song_ids in keyed.values() if len(song_ids) > 1]


def summarise(conn, clusters: list[list[int]], genre: str | None = None) -> dict:
    """What a merge would affect, without changing anything."""
    if genre is None:
        songs_before = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    else:
        songs_before = conn.execute(
            "SELECT COUNT(*) FROM songs s JOIN channels c ON c.channel_id = s.channel_id "
            "WHERE c.genre = ?",
            (genre,),
        ).fetchone()[0]
    removed = sum(len(ids) - 1 for ids in clusters)
    videos_touched = 0
    for ids in clusters:
        placeholders = ",".join("?" * len(ids))
        videos_touched += conn.execute(
            f"SELECT COUNT(*) FROM videos WHERE song_id IN ({placeholders})", ids
        ).fetchone()[0]

    return {
        "songs_before": songs_before,
        "clusters": len(clusters),
        "songs_after": songs_before - removed,
        "videos_touched": videos_touched,
    }


def describe(summary: dict) -> str:
    """The summary shown before anything is written."""
    lines = [
        "",
        "=" * 68,
        "  MERGE-VERSIONS - about to merge remix/acoustic/language/etc. version pairs",
        "=" * 68,
        f"  songs before           : {summary['songs_before']}",
        f"  clusters to merge      : {summary['clusters']}",
        f"  songs after            : {summary['songs_after']}",
        f"  videos reassigned      : {summary['videos_touched']}",
        "",
        "  Each cluster is a set of songs on the same anchor channel whose titles",
        "  now normalize to the same key - a remix/acoustic/language/etc. tag no",
        "  longer keeps them apart (see song_grouping.py's module docstring).",
        "  Survivor = lowest song_id; canonical_title resets to the shortest title",
        "  among the group's videos, per docs/manual-grouping-prompt.md's merge",
        "  convention. A timestamped backup is written before any change.",
        "=" * 68,
    ]
    return "\n".join(lines)


def merge_versions(genre: str | None = None, assume_yes: bool = False) -> int:
    """
    Merge every cluster find_clusters finds. Returns the number of song
    rows removed (i.e. songs merged away), or -1 if cancelled.
    """
    conn = db.get_connection()
    try:
        clusters = find_clusters(conn, genre=genre)
        if not clusters:
            print("  [merge_versions] Nothing to merge - no version pairs found.")
            return 0

        summary = summarise(conn, clusters, genre=genre)
        print(describe(summary))
        if not assume_yes:
            # A typed word rather than y/N, same as decouple.py - this
            # merges view counts across chart entries, not something to
            # confirm out of habit.
            answer = input("  Type 'merge' to confirm, anything else to cancel: ").strip()
            if answer != "merge":
                print("  [merge_versions] Cancelled - nothing was changed.")
                return -1

        backup = _write_backup()
        print(f"  [merge_versions] Backup written to {backup.name}")

        removed = 0
        for song_ids in clusters:
            survivor = min(song_ids)
            others = [sid for sid in song_ids if sid != survivor]
            placeholders = ",".join("?" * len(others))

            conn.execute(
                f"UPDATE videos SET song_id = ? WHERE song_id IN ({placeholders})",
                [survivor, *others],
            )
            shortest = conn.execute(
                "SELECT title FROM videos WHERE song_id = ? ORDER BY LENGTH(title) ASC LIMIT 1",
                (survivor,),
            ).fetchone()
            if shortest:
                conn.execute(
                    "UPDATE songs SET canonical_title = ? WHERE song_id = ?",
                    (shortest["title"], survivor),
                )
            conn.execute(f"DELETE FROM songs WHERE song_id IN ({placeholders})", others)
            removed += len(others)
        conn.commit()

        print(f"  [merge_versions] {len(clusters)} cluster(s) merged, {removed} song row(s) removed.")
        print("  [merge_versions] Videos are intact - only song_id reassignment and canonical_title changed.")
        return removed
    finally:
        conn.close()
