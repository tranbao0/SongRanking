"""
Generic chart engine: turns a named chart definition (data/charts.yaml)
into a ranked song list shaped exactly like youtube_api.search_kpop()'s
output, so chart_state.songs_from_search() consumes it unchanged. Adding
a chart is a new yaml entry; only a genuinely new metric needs a new
branch here.

Ranks by song, not by individual video: a song can have multiple YouTube
uploads (official MV, performance video, dance practice, etc.) that
song_grouping.py has already clustered via videos.song_id. Views are
summed across a group's members; one representative member (the
highest-individual-views one) supplies the url/title used for rendering.
Ungrouped videos (song_id NULL) are treated as their own singleton group,
so behavior for those is unchanged from before grouping existed.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from registry import db
from shared.dates import months_since

CHARTS_FILE = Path(__file__).parent.parent / "data" / "charts.yaml"


def _load_definition(name: str) -> dict:
    definitions = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))
    for entry in definitions:
        if entry["name"] == name:
            return entry
    raise ValueError(f"No chart named {name!r} in {CHARTS_FILE}")


def _video_rows(conn, genre: str) -> list:
    return conn.execute(
        """
        SELECT v.video_id, v.title, v.url, v.published_at, v.song_id,
               c.display_name AS uploader, s.canonical_title
        FROM videos v
        JOIN channels c ON c.channel_id = v.channel_id
        LEFT JOIN songs s ON s.song_id = v.song_id
        WHERE c.genre = ?
        """,
        (genre,),
    ).fetchall()


def _group_videos(videos: list) -> dict[str, list]:
    """Group video rows by song_id; an ungrouped video is its own singleton group."""
    groups: dict[str, list] = {}
    for v in videos:
        key = f"song:{v['song_id']}" if v["song_id"] is not None else f"video:{v['video_id']}"
        groups.setdefault(key, []).append(v)
    return groups


# Both variants resolve each video's snapshots with an index seek against
# view_snapshots' (video_id, snapshot_date) primary key rather than reading
# them. The straightforward "select every snapshot for the genre and keep
# the last one per video in Python" costs one row per video per day tracked
# - hundreds of thousands of rows within a few months of daily snapshots,
# every one of them materialised as a sqlite3.Row - to produce at most two
# numbers per video. These ask for exactly those two numbers instead, so
# cost scales with the video count and not with how long history has been
# accumulating.
_LATEST_VIEWS_SQL = """
    SELECT v.video_id,
           (SELECT s.views FROM view_snapshots s
             WHERE s.video_id = v.video_id
             ORDER BY s.snapshot_date DESC LIMIT 1) AS latest,
           NULL AS baseline
    FROM videos v
    JOIN channels c ON c.channel_id = v.channel_id
    WHERE c.genre = ?
"""

_LATEST_AND_BASELINE_VIEWS_SQL = """
    SELECT v.video_id,
           (SELECT s.views FROM view_snapshots s
             WHERE s.video_id = v.video_id
             ORDER BY s.snapshot_date DESC LIMIT 1) AS latest,
           (SELECT s.views FROM view_snapshots s
             WHERE s.video_id = v.video_id AND s.snapshot_date <= ?
             ORDER BY s.snapshot_date DESC LIMIT 1) AS baseline
    FROM videos v
    JOIN channels c ON c.channel_id = v.channel_id
    WHERE c.genre = ?
"""


def _latest_and_baseline_views(conn, genre: str, window_days: int | None) -> dict[str, dict]:
    """
    Per video_id: {"latest": int, "baseline": int | None}. baseline is the
    snapshot nearest but not after (today - window_days); None if there's
    no snapshot that old yet (too little history to compute an accurate
    delta, so callers should skip rather than guess).

    Videos with no snapshots at all are omitted entirely, so a caller
    can't mistake "never measured" for a real zero.
    """
    if window_days:
        cutoff = (date.today() - timedelta(days=window_days)).isoformat()
        rows = conn.execute(_LATEST_AND_BASELINE_VIEWS_SQL, (cutoff, genre))
    else:
        # No baseline is needed for cumulative/newest, so it isn't looked up.
        rows = conn.execute(_LATEST_VIEWS_SQL, (genre,))

    return {
        row["video_id"]: {"latest": row["latest"], "baseline": row["baseline"]}
        for row in rows
        if row["latest"] is not None
    }


def _build_group_entry(members: list, views_by_id: dict) -> dict | None:
    """
    Compute everything a chart might need from one song group: summed
    cumulative views, summed gained views (only from members that have
    both a latest and baseline snapshot - a too-new member is excluded
    from the sum rather than invalidating the whole group), the most
    recently published member (for "newest" ranking), and a
    representative member (highest individual views - supplies the
    url/title actually used for rendering). Returns None if no member has
    any view data yet.
    """
    representative = None
    best_latest = -1
    cumulative_total = 0
    gained_total = 0
    has_latest = False
    has_gained = False

    for v in members:
        data = views_by_id.get(v["video_id"])
        if data is None or data["latest"] is None:
            continue
        has_latest = True
        cumulative_total += data["latest"]
        if data["latest"] > best_latest:
            best_latest = data["latest"]
            representative = v
        if data["baseline"] is not None:
            has_gained = True
            gained_total += data["latest"] - data["baseline"]

    if not has_latest or representative is None:
        return None

    return {
        "representative": representative,
        "newest_member":  max(members, key=lambda v: v["published_at"]),
        "cumulative":      cumulative_total,
        "gained":          gained_total if has_gained else None,
    }


def compute_chart(name: str) -> list[dict]:
    definition = _load_definition(name)
    genre       = definition["genre"]
    metric      = definition["metric"]
    limit       = definition["limit"]
    window_days = definition.get("window_days")

    conn = db.get_connection()
    try:
        groups = _group_videos(_video_rows(conn, genre))

        # "gained" is the only metric that needs a baseline snapshot; the
        # others just want each video's latest view count.
        views_by_id = _latest_and_baseline_views(conn, genre, window_days if metric == "gained" else None)

        entries = [e for e in (_build_group_entry(m, views_by_id) for m in groups.values()) if e is not None]

        if metric == "newest":
            entries.sort(key=lambda e: e["newest_member"]["published_at"], reverse=True)
        elif metric == "cumulative":
            entries.sort(key=lambda e: e["cumulative"], reverse=True)
        elif metric == "gained":
            entries = [e for e in entries if e["gained"] is not None]
            entries.sort(key=lambda e: e["gained"], reverse=True)
        else:
            raise ValueError(f"Unknown chart metric: {metric!r}")

        results = []
        for entry in entries[:limit]:
            rep = entry["representative"]
            published_at = rep["published_at"][:10]
            release_date = datetime.strptime(published_at, "%Y-%m-%d").date()
            results.append({
                "id":              rep["video_id"],
                "title":           rep["canonical_title"] or rep["title"],
                "uploader":        rep["uploader"],
                "views":           entry["cumulative"],
                "upload_date":     release_date.strftime("%Y%m%d"),
                "duration":        0,
                "release_year":    release_date.year,
                "release_date":    release_date.strftime("%Y.%m.%d"),
                "years_on_chart":  max(1, date.today().year - release_date.year + 1),
                "months_on_chart": months_since(release_date),
                "url":             rep["url"],
            })
        return results
    finally:
        conn.close()
