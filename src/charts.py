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

Videos with song_id NULL are excluded entirely, not charted as their own
singleton: song_grouping always assigns a real MV a song_id, matching an
existing song or minting a new one, so NULL here only ever means a real
video decouple.py cleared that regroup.py hasn't re-grouped yet (blocked
non-song uploads never enter `videos` at all; see catalog.py's module
docstring) - it doesn't belong in a chart until it's grouped.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from registry import db, snapshot
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
        WHERE c.genre = ? AND v.song_id IS NOT NULL
        """,
        (genre,),
    ).fetchall()


def _group_videos(videos: list) -> dict[int, list]:
    """Group video rows by song_id."""
    groups: dict[int, list] = {}
    for v in videos:
        groups.setdefault(v["song_id"], []).append(v)
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


# Refreshing every tracked video before every chart computation costs one
# YouTube API unit per 50 videos regardless of how small the chart is (see
# shared.youtube_api.batch_fetch_metadata) - for a 200-song chart drawn
# from a genre with ~15,000 tracked videos, that's paying for a ~300-unit
# genre-wide sweep to serve a request that only needed a few hundred of
# them, and it only gets more lopsided as the registry grows. `sync`'s own
# daily pass already re-measures every tracked video regardless of what
# any chart needs, so it remains the backstop that guarantees nothing is
# missed for more than a day; this pool only has to hold what could
# plausibly change *this chart's* outcome right now.
_CANDIDATE_RECENT_DAYS = 90  # a viral new release - the "APT." case - is how a video jumps into contention between two `sync` runs
_CANDIDATE_BUFFER = 3        # top (limit * buffer) songs by last-known views: the only ones actually close enough to be competitive

_CANDIDATE_ROWS_SQL = """
    SELECT v.video_id, v.song_id, v.published_at,
           (SELECT s.views FROM view_snapshots s
             WHERE s.video_id = v.video_id
             ORDER BY s.snapshot_date DESC LIMIT 1) AS last_views
    FROM videos v
    JOIN channels c ON c.channel_id = v.channel_id
    WHERE c.genre = ? AND v.song_id IS NOT NULL
"""


def _candidate_video_ids(conn, genre: str, limit: int) -> list[str]:
    """
    Videos worth fetching a fresh view count for before computing this
    chart, rather than every video tracked in the genre - see the module
    comment above for why the rest is left to `sync`. A video qualifies
    if any of these hold:

      - never snapshotted: no baseline to even guess a rank from, so it
        could debut anywhere, including first.
      - published within _CANDIDATE_RECENT_DAYS: a new release going
        viral is how a video jumps into contention between two `sync`
        runs, and that's always a recent release, never a catalog title
        that's been sitting untouched for years.
      - a member of one of the current top (limit * _CANDIDATE_BUFFER)
        songs by last-known cumulative views (whatever the most recent
        snapshot on file says, even if stale) - the only songs actually
        close enough to the cutoff to matter.

    Every member of a qualifying song is included, not just the one that
    triggered it, so the group's cumulative total is refreshed
    consistently rather than mixing one fresh member with stale ones.
    """
    rows = conn.execute(_CANDIDATE_ROWS_SQL, (genre,)).fetchall()

    recent_cutoff = (date.today() - timedelta(days=_CANDIDATE_RECENT_DAYS)).isoformat()
    candidates: set[str] = set()
    groups: dict[int, list] = {}

    for r in rows:
        if r["last_views"] is None or r["published_at"][:10] >= recent_cutoff:
            candidates.add(r["video_id"])
        groups.setdefault(r["song_id"], []).append(r)

    ranked_groups = sorted(
        groups.values(),
        key=lambda members: sum(r["last_views"] or 0 for r in members),
        reverse=True,
    )
    for members in ranked_groups[:limit * _CANDIDATE_BUFFER]:
        candidates.update(r["video_id"] for r in members)

    return list(candidates)


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


def compute_chart(name: str, limit: int | None = None) -> list[dict]:
    """
    `limit`, when given, overrides the chart definition's own limit (e.g.
    the CLI's --limit) - narrows the candidate-refresh pool and how many
    entries are returned, without touching data/charts.yaml.
    """
    definition = _load_definition(name)
    genre       = definition["genre"]
    metric      = definition["metric"]
    limit       = limit if limit is not None else definition["limit"]
    window_days = definition.get("window_days")

    # A direct remote read, not the local-working-copy pull/push cycle
    # sync/decouple/regroup use - charts.py doesn't mutate the registry,
    # and this is a handful of aggregate queries, not the thousands of
    # small per-video/per-channel writes that made a local copy worth it
    # for sync (see db.py's module docstring).
    conn = db.get_remote_connection()
    try:
        # Charts read view_snapshots, but nothing else in the chart path
        # takes one - only `sync` did, so a chart run following a sync
        # that never reached its snapshot step (quota exhaustion, a
        # crash, or simply never having been run) silently computed
        # against zero view data instead of surfacing that. Narrowed to a
        # candidate pool rather than every tracked video - see
        # _candidate_video_ids - and a no-op for anything in that pool
        # that already has today's snapshot.
        candidate_ids = _candidate_video_ids(conn, genre, limit)
        print(f"  Refreshing view data for genre {genre!r} "
              f"({len(candidate_ids)} candidate video(s))...")
        snapshot.take_snapshot(video_ids=candidate_ids)

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
