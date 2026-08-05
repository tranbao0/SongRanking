"""
Daily view-count snapshot: records today's view count for every tracked
video. This is the real historical data charts.py computes windowed
metrics (e.g. "views gained in N days") from, rather than approximating
from publish date.

Rows are committed as each video's metadata arrives, not in one batch at
the end: a yt-dlp fallback run is throttle-paced and can take a long
time, and without incremental commits, killing (or losing to a bot-check
wall) a run that's an hour in would discard everything it had already
fetched. Idempotent inserts plus the pending-videos query already make a
partial run safe to resume, so committing early costs nothing.
"""

from datetime import date

from registry import db
from shared.youtube_api import batch_fetch_metadata


def take_snapshot(genre: str | None = None, video_ids: list[str] | None = None) -> int:
    """
    Fetch current view counts for tracked videos and insert today's row
    into view_snapshots. Idempotent - videos that already have a row for
    today are skipped, so re-running the same day is a no-op. Returns the
    number of snapshot rows inserted.

    Videos with song_id NULL are skipped entirely - per catalog.py's
    module docstring, that state unambiguously means a real video
    decouple.py cleared and regroup.py hasn't re-grouped yet (blocked
    non-song uploads never enter `videos` at all; they go to
    rejected_videos instead). It doesn't belong in a chart yet, so
    spending a fetch on it before it's grouped is premature.

    Scope narrows in two independent, combinable ways:
      - `genre`: just the videos on that genre's channels (used by `sync`,
        which wants every tracked video, genre by genre).
      - `video_ids`: just these specific videos, regardless of genre (used
        by charts.py to refresh a bounded candidate pool - the current
        contenders plus anything new - rather than its whole genre; see
        charts._candidate_video_ids).
    Neither given snapshots everything.
    """
    if video_ids is not None and not video_ids:
        return 0  # empty candidate set - nothing to do, and an empty IN() is invalid SQL

    today = date.today().isoformat()
    conn = db.get_connection()
    try:
        # Resolved in SQL (via idx_snapshots_date) rather than by loading
        # today's snapshot rows and every tracked video into Python and
        # differencing them - on a re-run later the same day that read back
        # the whole day's inserts just to discard all of them.
        scope = ""
        params: dict = {"today": today}
        if genre is not None:
            scope += " AND v.channel_id IN (SELECT channel_id FROM channels WHERE genre = :genre)"
            params["genre"] = genre
        if video_ids is not None:
            placeholders = ",".join(f":vid{i}" for i in range(len(video_ids)))
            scope += f" AND v.video_id IN ({placeholders})"
            params.update({f"vid{i}": vid for i, vid in enumerate(video_ids)})

        pending = conn.execute(
            f"""
            SELECT v.video_id, v.url
            FROM videos v
            WHERE v.song_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM view_snapshots s
                WHERE s.video_id = v.video_id AND s.snapshot_date = :today
            ){scope}
            """,
            params,
        ).fetchall()
        if not pending:
            print("  [snapshot] Nothing pending, all tracked videos already have today's snapshot")
            return 0

        print(f"  [snapshot] Fetching current views for {len(pending)} video(s)...")
        video_id_by_url = {v["url"]: v["video_id"] for v in pending}
        inserted = 0

        def _persist(url: str, meta: dict) -> None:
            nonlocal inserted
            conn.execute(
                "INSERT OR IGNORE INTO view_snapshots (video_id, snapshot_date, views) "
                "VALUES (?, ?, ?)",
                (video_id_by_url[url], today, meta["views"]),
            )
            conn.commit()
            inserted += 1

        batch_fetch_metadata([v["url"] for v in pending], on_result=_persist)

        print(f"  [snapshot] Inserted {inserted} snapshot row(s)")
        return inserted
    finally:
        conn.close()
