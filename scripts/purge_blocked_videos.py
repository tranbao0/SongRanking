"""
One-off backfill for the rejected_videos schema change (see db.py's
schema comment on that table and catalog.py's module docstring).

Before that change, catalog.py stored title-blocked uploads as `videos`
rows with song_id NULL instead of dropping them - which is exactly the
state regroup.py's "song_id IS NULL means needs grouping" assumption
couldn't tell apart from a real video decouple.py had cleared. Any
registry synced before the fix can have both:

  - Blocked videos still sitting in `videos` with song_id NULL, never
    picked up by anything, but occupying a row they no longer should.
  - Blocked videos that a `regroup` run already (wrongly) assigned a
    real song_id to, which now also need that grouping undone.

This walks every row in `videos`, re-checks it against the *current*
is_blocked_title, and for anything that fails: clears its song_id (if
any), moves it into rejected_videos, deletes it from `videos`, and at
the end drops any song row left holding nothing.

Only re-checks title, not duration - a stored row has no persisted
duration, and re-fetching one for ~75k videos to re-check would cost
real YouTube quota for what's already a title-driven cleanup.

Run against the local working copy (data/.registry_working.db) only.
Does not push to the hosted Turso registry - that's a separate,
explicit step once the result here has been reviewed.
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from registry import db  # noqa: E402
from shared.mv_filter import is_blocked_title  # noqa: E402


def main():
    if not db.LOCAL_WORKING_DB_PATH.exists():
        print(f"No local working copy at {db.LOCAL_WORKING_DB_PATH} - nothing to do.")
        return

    backup = db.LOCAL_WORKING_DB_PATH.parent / (
        f"registry.{datetime.now().strftime('%Y%m%d-%H%M%S')}.pre-purge-blocked.db"
    )
    shutil.copy2(db.LOCAL_WORKING_DB_PATH, backup)
    print(f"Backup written to {backup}")

    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT video_id, channel_id, title, song_id FROM videos").fetchall()
        total = len(rows)
        print(f"{total} video(s) in the local working copy.")

        to_move = [r for r in rows if is_blocked_title(r["title"])]
        print(f"{len(to_move)} video(s) fail the current filter and will be moved to rejected_videos.")

        was_grouped = sum(1 for r in to_move if r["song_id"] is not None)
        print(f"  of those, {was_grouped} currently hold a song_id (a bogus regroup result being undone).")

        now = datetime.now().isoformat()
        conn.executemany(
            """
            INSERT INTO rejected_videos (video_id, channel_id, title, checked_at)
            VALUES (:video_id, :channel_id, :title, :checked_at)
            ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, checked_at=excluded.checked_at
            """,
            [{"video_id": r["video_id"], "channel_id": r["channel_id"], "title": r["title"], "checked_at": now}
             for r in to_move],
        )
        conn.executemany(
            "DELETE FROM videos WHERE video_id = ?",
            [(r["video_id"],) for r in to_move],
        )
        cursor = conn.execute(
            "DELETE FROM songs WHERE song_id NOT IN (SELECT song_id FROM videos WHERE song_id IS NOT NULL)"
        )
        orphaned = cursor.rowcount
        conn.commit()

        remaining = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        print(f"Done. {len(to_move)} video(s) moved, {orphaned} orphaned song row(s) removed.")
        print(f"{remaining} video(s) remain in `videos`.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
