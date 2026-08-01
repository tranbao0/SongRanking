"""
Detaches videos from their songs so grouping can be redone from scratch.

Grouping is additive by design: a video's song_id is set once and never
re-derived, so a sync only ever spends work on videos that don't have one.
That is what keeps a daily sync cheap, but it also means a registry
grouped under settings you've since changed - or grouped without the AI
tier because a budget ran out mid-run - can't be improved in place. This
is the escape hatch: clear the assignments and let them be made again.

What that costs is not symmetric, and the confirmation prompt leads with
the part that is expensive:

  - A song holding one video loses nothing of value. Re-deriving it is a
    normalisation pass, done locally in microseconds.
  - A song holding several videos is a real decision - two uploads
    recognised as the same song. Some of those were paid for with API
    calls, and re-deriving them costs those calls again.

A timestamped backup is taken before anything is written, so an
accidental run is recoverable without re-spending any quota.
"""

import shutil
from datetime import datetime

from registry import db


def summarise(conn, genre: str | None = None) -> dict:
    """
    What a decouple would affect, without changing anything.

    `multi_video_songs` is the number that matters: those are the actual
    grouping decisions being discarded, as opposed to singletons whose
    song row is just bookkeeping.
    """
    scope = "" if genre is None else (
        " WHERE channel_id IN (SELECT channel_id FROM channels WHERE genre = :genre)"
    )
    params = {} if genre is None else {"genre": genre}

    videos = conn.execute(
        f"SELECT COUNT(*) FROM videos{scope}" if scope else "SELECT COUNT(*) FROM videos", params
    ).fetchone()[0]
    grouped = conn.execute(
        (f"SELECT COUNT(*) FROM videos{scope} AND song_id IS NOT NULL" if scope
         else "SELECT COUNT(*) FROM videos WHERE song_id IS NOT NULL"),
        params,
    ).fetchone()[0]

    song_rows = conn.execute(
        f"""SELECT song_id, COUNT(*) AS n FROM videos
            {scope + ' AND' if scope else 'WHERE'} song_id IS NOT NULL
            GROUP BY song_id""",
        params,
    ).fetchall()
    multi = [r for r in song_rows if r["n"] > 1]

    return {
        "videos": videos,
        "grouped_videos": grouped,
        "songs": len(song_rows),
        "multi_video_songs": len(multi),
        "videos_in_multi": sum(r["n"] for r in multi),
    }


def describe(summary: dict, genre: str | None) -> str:
    """The warning shown before anything is written."""
    scope = "every genre" if genre is None else f"genre {genre!r}"
    lines = [
        "",
        "=" * 68,
        f"  DECOUPLE - about to clear song assignments for {scope}",
        "=" * 68,
        f"  videos affected        : {summary['grouped_videos']} of {summary['videos']}",
        f"  song rows removed      : {summary['songs']}",
        "",
        f"  of those, {summary['multi_video_songs']} song(s) currently group "
        f"{summary['videos_in_multi']} video(s) together.",
        "  Those are real grouping decisions, and any that were made by the",
        "  AI tier will cost API calls to make again. If you don't intend to",
        "  re-group by hand, and no longer have the budget to re-run the AI",
        "  tier, stop here - this is not recoverable by syncing again,",
        "  because a sync only groups videos it has never seen before.",
        "",
        "  Everything else is singletons, which cost nothing to re-derive.",
        "  A timestamped backup is written before any change.",
        "=" * 68,
    ]
    return "\n".join(lines)


def decouple(genre: str | None = None, assume_yes: bool = False) -> int:
    """
    Clear song_id on the in-scope videos and drop the song rows left
    empty. Returns the number of videos decoupled, or -1 if cancelled.
    """
    conn = db.get_connection()
    try:
        summary = summarise(conn, genre)
        if summary["grouped_videos"] == 0:
            print("  [decouple] Nothing is grouped in that scope - nothing to do.")
            return 0

        print(describe(summary, genre))
        if not assume_yes:
            # A typed word rather than y/N: this is not the prompt to
            # dismiss out of habit, and the cost of an accident is API
            # spend rather than a moment's inconvenience.
            answer = input("  Type 'decouple' to confirm, anything else to cancel: ").strip()
            if answer != "decouple":
                print("  [decouple] Cancelled - nothing was changed.")
                return -1

        backup = db.DB_PATH.with_suffix(
            f".{datetime.now().strftime('%Y%m%d-%H%M%S')}.pre-decouple.db"
        )
        shutil.copy2(db.DB_PATH, backup)
        print(f"  [decouple] Backup written to {backup.name}")

        scope = "" if genre is None else (
            " AND channel_id IN (SELECT channel_id FROM channels WHERE genre = :genre)"
        )
        params = {} if genre is None else {"genre": genre}

        conn.execute(f"UPDATE videos SET song_id = NULL WHERE song_id IS NOT NULL{scope}", params)
        # Only songs nothing points at any more: a song keeping videos in
        # another genre survives a genre-scoped decouple.
        conn.execute(
            "DELETE FROM songs WHERE song_id NOT IN "
            "(SELECT song_id FROM videos WHERE song_id IS NOT NULL)"
        )
        conn.commit()

        print(f"  [decouple] {summary['grouped_videos']} video(s) decoupled, "
              f"{summary['songs']} song row(s) removed.")
        print("  [decouple] Videos are intact and still catalogued - only the grouping is gone.")
        return summary["grouped_videos"]
    finally:
        conn.close()
