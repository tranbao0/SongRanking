"""
Per-channel video discovery: pulls each tracked channel's uploads via the
YouTube Data API, filters to official MVs with mv_filter, groups
same-song duplicates via song_grouping, and upserts survivors into the
`videos` table for snapshot.py to track going forward.

Steady-state runs are budget-aware and resumable:
  - A channel's uploads-playlist ID and current video count are fetched
    in one combined call (1 unit total). If the count hasn't changed
    since last sync, that's the entire cost for this channel — no
    pagination at all.
  - When something *has* changed, pagination stops as soon as it reaches
    a video already in the `videos` table — the uploads playlist is
    newest-first, so anything after that point was already seen on a
    prior sync and doesn't need re-fetching.
  - Channels are processed oldest-synced-first and committed one at a
    time, so a run interrupted by QuotaExceededError (or split across a
    multi-day bootstrap by choice) picks up exactly where it left off
    next time instead of re-walking already-finished channels.
"""

import re
from datetime import date, datetime

import api_budget
import db
import song_grouping
from mv_filter import is_valid_mv
from youtube_api import _get_client, _parse_iso_duration

# Channels above this video count are treated as likely shared/label
# channels (e.g. "HYBE LABELS" hosting many different acts) rather than
# one artist's own channel. Only kworb-sourced channels get this
# treatment — kworb has no genre awareness and can resolve an artist
# search to a shared channel that also hosts artists outside the target
# genre. Wikidata-sourced channels skip this: cross-checked separately,
# real multi-artist overlap there is rare and already same-genre related
# acts (e.g. a member's channel doubling as their group's channel).
_SHARED_CHANNEL_VIDEO_THRESHOLD = 400


def _channel_status(youtube, channel_id: str) -> tuple[str | None, int | None]:
    """
    One call returning both the uploads playlist ID and the channel's
    current public video count (1 unit total, instead of two separate
    1-unit calls) — used for the cheap unchanged-channel skip and, when
    something did change, to seed the paginated walk below.
    """
    response = youtube.channels().list(part="contentDetails,statistics", id=channel_id).execute()
    api_budget.record_youtube_units(1)
    items = response.get("items", [])
    if not items:
        return None, None
    playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    count = items[0]["statistics"].get("videoCount")
    return playlist_id, (int(count) if count is not None else None)


def _list_new_uploads(youtube, playlist_id: str, known_video_ids: set[str]) -> list[dict]:
    """
    Paginate playlistItems.list, returning [{video_id, title, published_at}]
    for videos not already in `known_video_ids`. Stops as soon as a known
    video is reached rather than walking the channel's full history.
    """
    videos, page_token = [], None
    while True:
        response = youtube.playlistItems().list(
            part="snippet", playlistId=playlist_id, maxResults=50, pageToken=page_token,
        ).execute()
        api_budget.record_youtube_units(1)

        hit_known = False
        for item in response.get("items", []):
            snippet  = item["snippet"]
            video_id = snippet["resourceId"]["videoId"]
            if video_id in known_video_ids:
                hit_known = True
                break
            videos.append({
                "video_id":     video_id,
                "title":        snippet["title"],
                "published_at": snippet["publishedAt"],
            })

        page_token = response.get("nextPageToken")
        if hit_known or not page_token:
            break
    return videos


def _fetch_durations(youtube, video_ids: list[str]) -> dict[str, int]:
    durations = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        response = youtube.videos().list(part="contentDetails", id=",".join(chunk)).execute()
        api_budget.record_youtube_units(1)
        for item in response.get("items", []):
            durations[item["id"]] = _parse_iso_duration(item["contentDetails"].get("duration", ""))
    return durations


def _confirmed_artists_by_genre(conn) -> dict[str, list[re.Pattern]]:
    """
    Word-boundary regex per Wikidata-confirmed artist, grouped by genre.
    Used to filter large kworb-sourced channels down to videos that
    actually belong to a genre-confirmed artist (see module docstring).
    """
    rows = conn.execute("SELECT genre, display_name FROM channels WHERE source = 'wikidata'").fetchall()
    patterns: dict[str, list[re.Pattern]] = {}
    for row in rows:
        name = row["display_name"].strip()
        if not name:
            continue
        patterns.setdefault(row["genre"], []).append(re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE))
    return patterns


def _matches_confirmed_artist(title: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(title) for p in patterns)


def sync_videos(channel_ids: list[str] | None = None) -> int:
    """
    Discover new uploads for the given channels (all tracked channels,
    oldest-synced-first, if None), filter to official MVs, group same-song
    duplicates (across each channel's full catalog, old + new — a new
    upload might belong to an existing song group), and upsert into the
    videos table. Stops early (without crashing) if the YouTube API budget
    is exhausted — channels completed so far are already committed and
    will be skipped on the next run via last_catalog_sync ordering.
    Returns the number of new videos upserted.
    """
    conn = db.get_connection()
    try:
        query = "SELECT channel_id, genre, source, last_known_video_count FROM channels"
        params: list = []
        if channel_ids is not None:
            placeholders = ",".join("?" for _ in channel_ids)
            query += f" WHERE channel_id IN ({placeholders})"
            params = list(channel_ids)
        query += " ORDER BY last_catalog_sync ASC NULLS FIRST"
        rows = conn.execute(query, params).fetchall()

        artist_patterns = _confirmed_artists_by_genre(conn)
        youtube = _get_client()
        upserted = 0
        processed = 0

        for row in rows:
            channel_id = row["channel_id"]
            try:
                playlist_id, video_count = _channel_status(youtube, channel_id)
                if playlist_id is None:
                    continue

                if video_count is not None and video_count == row["last_known_video_count"]:
                    conn.execute(
                        "UPDATE channels SET last_catalog_sync = ? WHERE channel_id = ?",
                        (datetime.now().isoformat(), channel_id),
                    )
                    conn.commit()
                    processed += 1
                    continue

                existing = conn.execute(
                    "SELECT video_id, title, url, published_at, discovered_at FROM videos WHERE channel_id = ?",
                    (channel_id,),
                ).fetchall()
                existing_ids = {r["video_id"] for r in existing}

                new_uploads = _list_new_uploads(youtube, playlist_id, existing_ids)
                new_mvs = []
                if new_uploads:
                    durations = _fetch_durations(youtube, [v["video_id"] for v in new_uploads])
                    new_mvs = [
                        v for v in new_uploads
                        if is_valid_mv(v["title"], durations.get(v["video_id"], 0))
                    ]

                    if row["source"] == "kworb" and (video_count or 0) > _SHARED_CHANNEL_VIDEO_THRESHOLD:
                        patterns = artist_patterns.get(row["genre"], [])
                        if patterns:
                            before = len(new_mvs)
                            new_mvs = [v for v in new_mvs if _matches_confirmed_artist(v["title"], patterns)]
                            if len(new_mvs) != before:
                                print(f"  [catalog] {channel_id}: filtered {before - len(new_mvs)} video(s) "
                                      f"not matching a confirmed {row['genre']} artist (likely shared channel)")

                all_videos = [dict(r) for r in existing] + [
                    {
                        "video_id":      v["video_id"],
                        "title":         v["title"],
                        "url":           f"https://www.youtube.com/watch?v={v['video_id']}",
                        "published_at":  v["published_at"],
                        "discovered_at": date.today().isoformat(),
                    }
                    for v in new_mvs
                ]

                song_map = song_grouping.group_channel_videos(conn, channel_id, all_videos)

                rows_to_upsert = [
                    {**v, "channel_id": channel_id, "song_id": song_map.get(v["video_id"])}
                    for v in all_videos
                ]
                conn.executemany(
                    """
                    INSERT INTO videos (video_id, channel_id, title, url, published_at, discovered_at, song_id)
                    VALUES (:video_id, :channel_id, :title, :url, :published_at, :discovered_at, :song_id)
                    ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, song_id=excluded.song_id
                    """,
                    rows_to_upsert,
                )
                conn.execute(
                    "UPDATE channels SET last_catalog_sync = ?, last_known_video_count = ? WHERE channel_id = ?",
                    (datetime.now().isoformat(), video_count, channel_id),
                )
                conn.commit()
                upserted += len(new_mvs)
                processed += 1

            except api_budget.QuotaExceededError as e:
                print(f"  [catalog] {e}")
                print(f"  [catalog] Stopped after {processed}/{len(rows)} channel(s) — "
                      f"already-synced channels will be skipped on the next `sync` run.")
                break

        return upserted
    finally:
        conn.close()
