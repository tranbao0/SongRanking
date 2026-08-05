"""
One-off backfill: writes a view_snapshots row for every video that has
none at all, regardless of song_id. Unlike snapshot.take_snapshot (which
deliberately skips song_id IS NULL videos - real videos awaiting
(re-)grouping, per catalog.py's module docstring - since they don't
belong in a chart yet), this fills in the gap so every video row has at
least one historical view count on record before it's grouped.
"""

from datetime import date

from registry import db
from shared.youtube_api import batch_fetch_metadata


def main() -> None:
    today = date.today().isoformat()
    conn = db.get_connection()
    try:
        pending = conn.execute(
            """
            SELECT v.video_id, v.url
            FROM videos v
            WHERE NOT EXISTS (
                SELECT 1 FROM view_snapshots s WHERE s.video_id = v.video_id
            )
            """
        ).fetchall()
        if not pending:
            print("Nothing pending, every video already has a view_snapshots entry")
            return

        print(f"Fetching current views for {len(pending)} video(s)...")
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

        print(f"Inserted {inserted} snapshot row(s)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
