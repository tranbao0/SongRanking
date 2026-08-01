"""
Per-channel video discovery: pulls each tracked channel's uploads via the
YouTube Data API, filters to official MVs with mv_filter, groups
same-song duplicates via song_grouping, and upserts survivors into the
`videos` table for snapshot.py to track going forward.

Steady-state runs are budget-aware and resumable:
  - A channel's uploads-playlist ID and current video count are fetched
    in one combined call (1 unit total). If the count hasn't changed
    since last sync, that's the entire cost for this channel - no
    pagination at all.
  - When something *has* changed, pagination stops as soon as it reaches
    a video already in the `videos` table - the uploads playlist is
    newest-first, so anything after that point was already seen on a
    prior sync and doesn't need re-fetching.
  - Channels are processed oldest-synced-first and committed one at a
    time, so a run interrupted by QuotaExceededError (or split across a
    multi-day bootstrap by choice) picks up exactly where it left off
    next time instead of re-walking already-finished channels.
"""

import re
from datetime import date, datetime

from registry import db, song_grouping
from shared import api_budget
from shared.mv_filter import is_blocked_title, is_valid_mv
from shared.youtube_api import _get_client, _parse_iso_duration

# Channels above this video count are treated as likely shared/label
# channels (e.g. "HYBE LABELS" hosting many different acts) rather than
# one artist's own channel. Only kworb-sourced channels get this
# treatment - kworb has no genre awareness and can resolve an artist
# search to a shared channel that also hosts artists outside the target
# genre. Wikidata-sourced channels skip this: cross-checked separately,
# real multi-artist overlap there is rare and already same-genre related
# acts (e.g. a member's channel doubling as their group's channel).
_SHARED_CHANNEL_VIDEO_THRESHOLD = 400

# How often (in pages) to print pagination progress for a large channel -
# without this, a channel with thousands of uploads walks silently for
# several minutes, which looks identical to a hang.
_PAGE_LOG_INTERVAL = 10


def _channel_status(youtube, channel_id: str) -> tuple[str | None, int | None]:
    """
    One call returning both the uploads playlist ID and the channel's
    current public video count (1 unit total, instead of two separate
    1-unit calls) - used for the cheap unchanged-channel skip and, when
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
    videos, page_token, page_num = [], None, 0
    while True:
        response = youtube.playlistItems().list(
            part="snippet", playlistId=playlist_id, maxResults=50, pageToken=page_token,
        ).execute()
        api_budget.record_youtube_units(1)
        page_num += 1
        if page_num % _PAGE_LOG_INTERVAL == 0:
            print(f"    ...page {page_num} ({len(videos)} new video(s) so far, still paginating)")

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


# A song is recorded against its artist's own channel when one is known,
# even though the video that created it may sit on an aggregator or
# broadcast channel that happened to upload it first (see song_grouping's
# artist_home_channels). Matching only on videos.channel_id would never
# find those, so the artist's own upload of the same song would start a
# second song rather than joining the existing one - and on a bootstrap
# the aggregators are exactly the channels that get catalogued first,
# since kworb entries are inserted ahead of Wikidata's.
# song_channel_id (the anchor) rides along so song_grouping can tell which
# artist each candidate song belongs to without re-parsing its title -
# an artist's own upload is often just the song name, with the artist
# nowhere in the text.
_EXISTING_VIDEOS_SQL = """
    SELECT v.video_id, v.title, v.url, v.published_at, v.discovered_at, v.song_id,
           s.channel_id AS song_channel_id
    FROM videos v
    LEFT JOIN songs s ON s.song_id = v.song_id
    WHERE v.channel_id = :channel_id
    UNION
    SELECT v.video_id, v.title, v.url, v.published_at, v.discovered_at, v.song_id,
           s.channel_id
    FROM videos v
    JOIN songs s ON s.song_id = v.song_id
    WHERE s.channel_id = :channel_id
"""


def _existing_videos(conn, channel_id: str) -> list:
    """
    Everything a new upload on `channel_id` should be matched against:
    that channel's own videos, plus any video belonging to a song already
    anchored to it. UNION rather than UNION ALL, so a video that satisfies
    both halves is offered once.
    """
    return conn.execute(_EXISTING_VIDEOS_SQL, {"channel_id": channel_id}).fetchall()


def _confirmed_artists(conn) -> tuple[dict[str, list[tuple[str, re.Pattern]]], dict[str, str]]:
    """
    One pass over the Wikidata-confirmed channels, returning both things
    sync_videos needs from them:

      - {genre: [(artist_name, title-matching regex), ...]}, compiled by
        song_grouping.artist_pattern so the regexes here and the combined
        prefilter built from them anchor identically. Used both to filter
        large kworb-sourced channels down to videos that actually
        belong to a genre-confirmed artist (see
        _SHARED_CHANNEL_VIDEO_THRESHOLD above), and - passed through to
        song_grouping as artist_patterns - to tag which artist a title
        belongs to, so grouping can't merge two different artists'
        same-titled songs together on a channel that hosts more than one.
      - {artist_name: that artist's own channel_id}. Passed to
        song_grouping as artist_home_channels so a video matched to a
        confirmed artist on a *different* channel (e.g. a broadcast/stage
        channel re-uploading them) can be recognized as the same artist's
        song instead of becoming a separate, un-merged entry - see
        song_grouping's module docstring.

    Previously two separate queries over the same rows. Row order (and so
    which channel_id wins when two confirmed channels share a display_name)
    is unchanged, since both queries had the same table and WHERE clause.
    """
    rows = conn.execute(
        "SELECT genre, display_name, channel_id FROM channels WHERE source = 'wikidata'"
    ).fetchall()
    patterns: dict[str, list[tuple[str, re.Pattern]]] = {}
    home_channels: dict[str, str] = {}
    for row in rows:
        name = row["display_name"].strip()
        if not name:
            continue
        patterns.setdefault(row["genre"], []).append((name, song_grouping.artist_pattern(name)))
        home_channels[name] = row["channel_id"]
    return patterns, home_channels


def sync_videos(channel_ids: list[str] | None = None) -> int:
    """
    Discover new uploads for the given channels (all tracked channels,
    oldest-synced-first, if None), filter to official MVs, group same-song
    duplicates (only new uploads are (re-)classified - see song_grouping's
    module docstring), and upsert into the videos table. Stops early
    (without crashing) if the YouTube API budget is exhausted - channels
    completed so far are already committed and will be skipped on the
    next run via last_catalog_sync ordering.
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

        total = len(rows)
        print(f"  [catalog] {total} channel(s) to check")

        artist_patterns, official_channels = _confirmed_artists(conn)
        # Built once per genre rather than per channel: every shared channel
        # in a genre matches against the same confirmed-artist list, and a
        # shared index also shares its compiled prefilter and per-title memo
        # (see song_grouping._ArtistIndex).
        artist_index_by_genre = {
            genre: song_grouping.build_artist_index(patterns)
            for genre, patterns in artist_patterns.items()
        }
        no_artist_index = song_grouping.build_artist_index([])
        youtube = _get_client()
        upserted = 0
        processed = 0

        for i, row in enumerate(rows, start=1):
            channel_id = row["channel_id"]
            progress = f"[catalog {i}/{total}]"
            try:
                playlist_id, video_count = _channel_status(youtube, channel_id)
                if playlist_id is None:
                    print(f"  {progress} {channel_id}: no uploads playlist (channel deleted/private?), skipping")
                    continue

                if video_count is not None and video_count == row["last_known_video_count"]:
                    print(f"  {progress} {channel_id}: unchanged ({video_count} videos), skipped")
                    conn.execute(
                        "UPDATE channels SET last_catalog_sync = ? WHERE channel_id = ?",
                        (datetime.now().isoformat(), channel_id),
                    )
                    conn.commit()
                    processed += 1
                    continue

                print(f"  {progress} {channel_id}: video count changed "
                      f"({row['last_known_video_count']} -> {video_count}), walking new uploads...")

                existing = _existing_videos(conn, channel_id)
                existing_ids = {r["video_id"] for r in existing}

                # Non-empty only for large kworb-sourced channels (see
                # _SHARED_CHANNEL_VIDEO_THRESHOLD) - those are the only ones
                # where a title's artist isn't already implied by channel_id
                # alone, so this is the only case where song_grouping needs
                # per-title artist tagging to avoid merging two different
                # artists' same-titled songs.
                shared_index = (
                    artist_index_by_genre.get(row["genre"], no_artist_index)
                    if row["source"] == "kworb" and (video_count or 0) > _SHARED_CHANNEL_VIDEO_THRESHOLD
                    else no_artist_index
                )

                new_uploads = _list_new_uploads(youtube, playlist_id, existing_ids)
                new_mvs = []
                if new_uploads:
                    # Every rejection that needs only the title runs before
                    # any duration is fetched. Durations cost a quota unit
                    # per 50 videos - the same rate as walking the playlist
                    # itself - so anything already ruled out is quota spent
                    # on an answer that gets discarded. The survivors are
                    # duration-checked identically below, so the resulting
                    # set is unchanged; only what it cost to reach it is.
                    #
                    # This matters most exactly where the bill is largest.
                    # A broadcast archive can carry tens of thousands of
                    # uploads of which only a small fraction name a
                    # confirmed artist at all, and that check is pure text.
                    candidates = [v for v in new_uploads if not is_blocked_title(v["title"])]

                    if shared_index:
                        before = len(candidates)
                        candidates = [v for v in candidates if shared_index.match(v["title"]) is not None]
                        if len(candidates) != before:
                            print(f"    filtered {before - len(candidates)} video(s) not matching a "
                                  f"confirmed {row['genre']} artist (likely shared channel)")

                    durations = _fetch_durations(youtube, [v["video_id"] for v in candidates])
                    new_mvs = [
                        v for v in candidates
                        if is_valid_mv(v["title"], durations.get(v["video_id"], 0))
                    ]

                new_video_dicts = [
                    {
                        "video_id":      v["video_id"],
                        "title":         v["title"],
                        "url":           f"https://www.youtube.com/watch?v={v['video_id']}",
                        "published_at":  v["published_at"],
                        "discovered_at": date.today().isoformat(),
                    }
                    for v in new_mvs
                ]

                # On a shared channel, a new video's matched artist might
                # already have their own tracked channel elsewhere - pull
                # that channel's existing videos in too so this upload can
                # link to that artist's existing song instead of becoming
                # a separate entry (see song_grouping's module docstring).
                cross_channel_ids = set()
                if shared_index:
                    for v in new_video_dicts:
                        artist = shared_index.match(v["title"])
                        home_channel_id = official_channels.get(artist) if artist else None
                        if home_channel_id and home_channel_id != channel_id:
                            cross_channel_ids.add(home_channel_id)

                cross_existing = []
                for cid in cross_channel_ids:
                    cross_existing.extend(_existing_videos(conn, cid))

                # existing videos already have a settled song_id and are never
                # re-grouped - only new_video_dicts goes through song_grouping,
                # so AI usage scales with today's new uploads, not the channel's
                # whole history.
                if new_video_dicts:
                    extra = f" (cross-checking {len(cross_channel_ids)} artist channel(s))" if cross_channel_ids else ""
                    print(f"    grouping {len(new_video_dicts)} new video(s) into songs...{extra}")
                song_map = song_grouping.group_channel_videos(
                    conn, channel_id, existing + cross_existing, new_video_dicts,
                    artist_patterns=shared_index, artist_home_channels=official_channels,
                )

                rows_to_upsert = [
                    {**v, "channel_id": channel_id, "song_id": song_map.get(v["video_id"])}
                    for v in new_video_dicts
                ]
                if rows_to_upsert:
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
                print(f"    done - {len(new_mvs)} new video(s) upserted")

            except api_budget.QuotaExceededError as e:
                print(f"  [catalog] {e}")
                print(f"  [catalog] Stopped after {processed}/{total} channel(s) - "
                      f"already-synced channels will be skipped on the next `sync` run.")
                break

        return upserted
    finally:
        conn.close()
