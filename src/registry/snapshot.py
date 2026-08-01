"""
Daily view-count snapshot: records today's view count for every tracked
video. This is the real historical data charts.py computes windowed
metrics (e.g. "views gained in N days") from, rather than approximating
from publish date.
"""

from datetime import date

from registry import db
from shared.youtube_api import batch_fetch_metadata


def take_snapshot() -> int:
    """
    Fetch current view counts for every tracked video and insert today's
    row into view_snapshots. Idempotent - videos that already have a row
    for today are skipped, so re-running the same day is a no-op.
    Returns the number of snapshot rows inserted.
    """
    today = date.today().isoformat()
    conn = db.get_connection()
    try:
        # Resolved in SQL (via idx_snapshots_date) rather than by loading
        # today's snapshot rows and every tracked video into Python and
        # differencing them - on a re-run later the same day that read back
        # the whole day's inserts just to discard all of them.
        pending = conn.execute(
            """
            SELECT v.video_id, v.url
            FROM videos v
            WHERE NOT EXISTS (
                SELECT 1 FROM view_snapshots s
                WHERE s.video_id = v.video_id AND s.snapshot_date = ?
            )
            """,
            (today,),
        ).fetchall()
        if not pending:
            print("  [snapshot] Nothing pending, all tracked videos already have today's snapshot")
            return 0

        print(f"  [snapshot] Fetching current views for {len(pending)} video(s)...")
        metadata = batch_fetch_metadata([v["url"] for v in pending])

        rows = [
            {"video_id": v["video_id"], "snapshot_date": today, "views": metadata[v["url"]]["views"]}
            for v in pending
            if v["url"] in metadata
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO view_snapshots (video_id, snapshot_date, views) "
            "VALUES (:video_id, :snapshot_date, :views)",
            rows,
        )
        conn.commit()
        print(f"  [snapshot] Inserted {len(rows)} snapshot row(s)")
        return len(rows)
    finally:
        conn.close()
