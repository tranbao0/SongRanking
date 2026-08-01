"""
Daily view-count snapshot: records today's view count for every tracked
video. This is the real historical data charts.py computes windowed
metrics (e.g. "views gained in N days") from, rather than approximating
from publish date.
"""

from datetime import date

import db
from youtube_api import batch_fetch_metadata


def take_snapshot() -> int:
    """
    Fetch current view counts for every tracked video and insert today's
    row into view_snapshots. Idempotent — videos that already have a row
    for today are skipped, so re-running the same day is a no-op.
    Returns the number of snapshot rows inserted.
    """
    today = date.today().isoformat()
    conn = db.get_connection()
    try:
        already_done = {
            row["video_id"]
            for row in conn.execute(
                "SELECT video_id FROM view_snapshots WHERE snapshot_date = ?", (today,)
            )
        }
        videos = conn.execute("SELECT video_id, url FROM videos").fetchall()
        pending = [v for v in videos if v["video_id"] not in already_done]
        if not pending:
            return 0

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
        return len(rows)
    finally:
        conn.close()
