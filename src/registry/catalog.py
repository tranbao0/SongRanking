"""
Per-channel video discovery: pulls each tracked channel's uploads via the
YouTube Data API, filters to official MVs with mv_filter, groups
same-song duplicates via song_grouping, and upserts survivors into the
`videos` table for snapshot.py to track going forward. Anything mv_filter
rejects (title or duration) is recorded in `rejected_videos` instead -
never in `videos` - so song_id NULL in `videos` unambiguously means "a
real video decouple.py cleared, still needing (re-)grouping" rather than
also meaning "not a song at all"; see rejected_videos' schema comment for
why that distinction matters to regroup.py.

Steady-state runs are budget-aware and resumable:
  - A channel's uploads-playlist ID and current video count are fetched
    in one combined call (1 unit total). If the count hasn't changed
    since last sync, that's the entire cost for this channel - no
    pagination at all.
  - When something *has* changed, pagination stops as soon as it reaches
    a video already in `videos` or `rejected_videos` - the uploads
    playlist is newest-first, so anything after that point was already
    seen (and, if rejected, already judged) on a prior sync and doesn't
    need re-fetching.
  - Channels are processed oldest-synced-first and committed one at a
    time, so a run interrupted by QuotaExceededError (or split across a
    multi-day bootstrap by choice) picks up exactly where it left off
    next time instead of re-walking already-finished channels.

Channel/playlist listing has no substitute and simply stops the run on
quota exhaustion, relying on the resumability above - a channel caught
mid-listing isn't lost, just retried next run. Duration-checking is
different: it shares youtube_api.batch_fetch_metadata with snapshot.py's
view-count fetch, so a chunk the quota guard turns away falls back to
yt-dlp (see _fetch_durations) instead of ending the run over what's
usually the smaller half of a channel's quota cost.

A channel tracked via discovery.py's manual-videos yaml (source
"manual_video") skips all of the above: it never lists a playlist, is
never MV-filtered, and is handled by _sync_manual_video_channel instead -
see that function's docstring.
"""

import re
from datetime import date, datetime

from registry import db, discovery, song_grouping
from shared import api_budget, shutdown
from shared.mv_filter import is_blocked_title, is_valid_mv
from shared.youtube_api import batch_fetch_metadata, chunked_ids, get_client

# Channels above this video count are treated as likely shared/label
# channels (e.g. "HYBE LABELS" or "1theK" hosting many different acts)
# rather than one artist's own channel.
#
# Wikidata-sourced channels are exempt: Wikidata links an artist to their
# own channel, so real multi-artist overlap there is rare and is already
# same-genre related acts (e.g. a member's channel doubling as their
# group's channel). Every other source is a curated addition - labels,
# distributors and broadcasters, which is exactly what a manual entry
# exists to add - so those do need per-title artist tagging to keep two
# different artists' same-titled songs from merging.
_SHARED_CHANNEL_VIDEO_THRESHOLD = 400

# How often (in pages) to print pagination progress for a large channel -
# without this, a channel with thousands of uploads walks silently for
# several minutes, which looks identical to a hang.
_PAGE_LOG_INTERVAL = 10

# Confirmed-artist names shorter than this are dropped from the matching
# roster. "I" and "N" are both genuinely Wikidata-confirmed kpop acts, but
# a one-character name matches the word "I" in any English title: measured
# on a real registry, "I" alone falsely confirmed 296 videos on channels
# that were not kpop at all, which was the bulk of the leak. Those two
# artists' own uploads are lost from shared channels as a result, which is
# the accepted cost of not mislabelling everything else as theirs.
_MIN_ARTIST_NAME_LENGTH = 2


def _channel_status(youtube, channel_id: str) -> tuple[str | None, int | None]:
    """
    One call returning both the uploads playlist ID and the channel's
    current public video count (1 unit total, instead of two separate
    1-unit calls) - used for the cheap unchanged-channel skip and, when
    something did change, to seed the paginated walk below.

    Single-channel fallback for _prefetch_channel_statuses: normal sync
    runs never call this directly (see sync_videos), only a channel whose
    batched chunk hit a transient, non-quota error during that prefetch.
    """
    response = youtube.channels().list(part="contentDetails,statistics", id=channel_id).execute()
    api_budget.record_youtube_units(1)
    items = response.get("items", [])
    if not items:
        return None, None
    playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    count = items[0]["statistics"].get("videoCount")
    return playlist_id, (int(count) if count is not None else None)


def _prefetch_channel_statuses(youtube, channel_ids: list[str]) -> dict[str, tuple[str | None, int | None]]:
    """
    Uploads-playlist ID + current video count for every channel_id, via
    channels().list's own id="a,b,c,...,50" batching - the same one unit
    per call regardless of ID count that videos.list already gets used
    for elsewhere (see youtube_api.py). A channel-by-channel loop calling
    _channel_status once per row costs one full unit per channel; this
    answers up to 50 at a time for that same one unit, so a run tracking
    ~600 channels spends ~12 units on status checks instead of ~600.

    Every ID gets an entry: (None, None) for one absent from a completed
    chunk's response (deleted/private channel - same as _channel_status's
    own empty-items case), or no entry at all for an ID whose chunk
    itself failed with something other than a quota error (rare -
    transient network blip). sync_videos falls back to _channel_status
    per-channel for exactly those missing IDs, so a blip during the
    prefetch never silently drops a channel for the whole run.

    A quota-exceeded error is NOT caught here - it propagates to
    sync_videos' caller, same as _channel_status raising one would have:
    every remaining chunk (and every remaining channel) would fail the
    same way, so the whole run stops there rather than limping through
    chunks that can't succeed. Because this is ~50x cheaper in quota than
    the per-channel version, hitting that wall during the prefetch itself
    is now a much narrower case than it used to be - see this function's
    caller for what "narrower" means for resumability.
    """
    statuses: dict[str, tuple[str | None, int | None]] = {}
    for chunk in chunked_ids(channel_ids):
        try:
            response = youtube.channels().list(part="contentDetails,statistics", id=",".join(chunk)).execute()
        except _HTTP_ERRORS as e:
            if _is_quota_error(e):
                raise
            print(f"  [catalog] Batched status check failed for {len(chunk)} channel(s) "
                  f"({e}) - will check them individually.")
            continue
        api_budget.record_youtube_units(1)
        found = {item["id"]: item for item in response.get("items", [])}
        for channel_id in chunk:
            item = found.get(channel_id)
            if item is None:
                statuses[channel_id] = (None, None)
            else:
                playlist_id = item["contentDetails"]["relatedPlaylists"]["uploads"]
                count = item["statistics"].get("videoCount")
                statuses[channel_id] = (playlist_id, int(count) if count is not None else None)
    return statuses


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
    """
    Duration (seconds) per video ID, via youtube_api.batch_fetch_metadata -
    the same chunked, concurrently-fetched call snapshot.py uses for view
    counts. Sharing it means a chunk turned away by the quota guard falls
    back to yt-dlp (bot-detection-paced - see metadata.py's _throttle)
    instead of this step ending the whole channel loop the moment quota
    runs out. `youtube` is unused - batch_fetch_metadata builds its own
    per-thread client - kept so this call site doesn't need to change.
    """
    if not video_ids:
        return {}
    url_by_id = {vid: f"https://www.youtube.com/watch?v={vid}" for vid in video_ids}
    fetched = batch_fetch_metadata(list(url_by_id.values()))
    return {
        vid: fetched[url]["duration"]
        for vid, url in url_by_id.items()
        if url in fetched
    }


# A song is recorded against its artist's own channel when one is known,
# even though the video that created it may sit on an aggregator or
# broadcast channel that happened to upload it first (see song_grouping's
# artist_home_channels). Matching only on videos.channel_id would never
# find those, so the artist's own upload of the same song would start a
# second song rather than joining the existing one - and on a bootstrap
# the aggregators are exactly the channels that get catalogued first,
# since a distributor's back catalogue dwarfs any single artist's.
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


try:
    from googleapiclient.errors import HttpError  # type: ignore
    _HTTP_ERRORS: tuple = (HttpError,)
except ImportError:  # googleapiclient is only needed for the API backend
    _HTTP_ERRORS = ()

# YouTube reasons meaning the day's quota is gone. Every remaining channel
# would fail the same way, so the run stops rather than burning through
# 600-odd channels logging the same error - and stopping leaves the
# already-committed ones to be skipped cheaply on the next run.
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}


def _is_quota_error(error) -> bool:
    reason = ""
    for detail in getattr(error, "error_details", None) or []:
        if isinstance(detail, dict):
            reason = detail.get("reason", "") or reason
    if not reason:
        reason = str(error)
    return any(r in reason for r in _QUOTA_REASONS)


def _mark_synced(conn, channel_id: str, video_count: int | None) -> None:
    """
    Record that this channel is done for now. Storing the count is what
    makes the next run cost one unit for it instead of a full walk.
    """
    conn.execute(
        "UPDATE channels SET last_catalog_sync = ?, last_known_video_count = ? WHERE channel_id = ?",
        (datetime.now().isoformat(), video_count, channel_id),
    )
    conn.commit()


def _existing_videos(conn, channel_id: str) -> list:
    """
    Everything a new upload on `channel_id` should be matched against:
    that channel's own videos, plus any video belonging to a song already
    anchored to it. UNION rather than UNION ALL, so a video that satisfies
    both halves is offered once.
    """
    return conn.execute(_EXISTING_VIDEOS_SQL, {"channel_id": channel_id}).fetchall()


def _rejected_video_ids(conn, channel_id: str) -> set[str]:
    """
    Video IDs already fetched and rejected on this channel (mv_filter
    failed on title or duration). Never in `videos` - see the schema
    comment on rejected_videos - so the pagination stop in sync_videos
    has to union this in on top of _existing_videos to still recognize
    them as already-seen and stop walking there.
    """
    return {
        r["video_id"]
        for r in conn.execute("SELECT video_id FROM rejected_videos WHERE channel_id = ?", (channel_id,))
    }


def _confirmed_artists(conn) -> tuple[dict[str, list[tuple[str, re.Pattern]]], dict[str, str]]:
    """
    One pass over the Wikidata-confirmed channels, returning both things
    sync_videos needs from them:

      - {genre: [(artist_name, title-matching regex), ...]}, compiled by
        song_grouping.artist_pattern so the regexes here and the combined
        prefilter built from them anchor identically. Used both to filter
        large multi-artist channels down to videos that actually
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
        if len(name) < _MIN_ARTIST_NAME_LENGTH:
            continue
        patterns.setdefault(row["genre"], []).append((name, song_grouping.artist_pattern(name)))
        home_channels[name] = row["channel_id"]
    return patterns, home_channels


def _sync_manual_video_channel(
    conn, youtube, channel_id: str, genre: str, sibling_channel_ids: list[str],
) -> int:
    """
    Fetch only this channel's individually pinned videos (discovery.py's
    manual-videos yaml) - never its upload history, since a "manual_video"
    source channel is typically mostly a different genre (a movie studio,
    a late-night show) and only one specific upload belongs here.

    `sibling_channel_ids` are every other "manual_video" channel in this
    genre, folded into the grouping context alongside this channel's own
    existing videos. Pinned videos of the same song routinely land on
    different real channels - a studio's official upload and a talk
    show's performance clip of it - and group_channel_videos only ever
    compares videos it's handed together, so without this a per-channel
    call could never recognize the two as one song. Skips MV title/
    duration filtering entirely: a pinned video is already a deliberate,
    one-by-one human choice, unlike a channel walk's mv_filter heuristics.
    """
    pinned = discovery.manual_videos(genre)
    own_pinned_ids = {v["video_id"] for v in pinned if v["channel_id"] == channel_id}
    existing = _existing_videos(conn, channel_id)
    known_ids = {r["video_id"] for r in existing}
    new_ids = [vid for vid in own_pinned_ids if vid not in known_ids]
    if not new_ids:
        return 0

    details: dict[str, dict] = {}
    for chunk in chunked_ids(new_ids):
        response = youtube.videos().list(part="snippet", id=",".join(chunk)).execute()
        api_budget.record_youtube_units(1)
        for item in response.get("items", []):
            details[item["id"]] = item["snippet"]

    new_video_dicts = [
        {
            "video_id":      vid,
            "title":         details[vid]["title"],
            "url":           f"https://www.youtube.com/watch?v={vid}",
            "published_at":  details[vid]["publishedAt"],
            "discovered_at": date.today().isoformat(),
        }
        for vid in new_ids if vid in details
    ]
    if not new_video_dicts:
        return 0

    sibling_existing = []
    for sibling_id in sibling_channel_ids:
        if sibling_id != channel_id:
            sibling_existing.extend(_existing_videos(conn, sibling_id))

    song_map = song_grouping.group_channel_videos(conn, channel_id, existing + sibling_existing, new_video_dicts)
    rows_to_upsert = [
        {**v, "channel_id": channel_id, "song_id": song_map.get(v["video_id"])}
        for v in new_video_dicts
    ]
    conn.executemany(
        """
        INSERT INTO videos (video_id, channel_id, title, url, published_at, discovered_at, song_id)
        VALUES (:video_id, :channel_id, :title, :url, :published_at, :discovered_at, :song_id)
        ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, song_id=excluded.song_id
        """,
        rows_to_upsert,
    )
    conn.commit()
    return len(new_video_dicts)


def sync_videos(channel_ids: list[str] | None = None, genres: list[str] | None = None) -> int:
    """
    Discover new uploads for the given channels (all tracked channels,
    oldest-synced-first, if neither scope is given), filter to official
    MVs, group same-song duplicates (only new uploads are (re-)classified -
    see song_grouping's module docstring), and upsert into the videos
    table. Stops early (without crashing) if the YouTube API budget is
    exhausted - channels completed so far are already committed and will
    be skipped on the next run via last_catalog_sync ordering.

    Every non-manual-video channel's status (uploads playlist + video
    count) is batch-fetched once up front via _prefetch_channel_statuses,
    before any channel's own walk/grouping/commit work starts - see that
    function's docstring for the quota math. This doesn't change the
    resumability of the expensive per-channel work below: that's still
    walked and committed one channel at a time, in the same order, so a
    QuotaExceededError there still stops with exactly as many channels
    already committed as today. What changes is only where the *status*
    check's own quota risk sits: it's now one cheap batch instead of up to
    `total` separate 1-unit calls interleaved through the loop, so hitting
    the budget wall during it (rather than during the walk) becomes the
    rare case, not the common one - and if it does happen, nothing has
    been processed yet regardless, exactly like a QuotaExceededError on
    this run's very first channel would have meant before.

    `channel_ids` and `genres` are independent, combinable scopes (both
    given narrows to their intersection) - same pattern as
    snapshot.take_snapshot's `genre`/`video_ids`. Neither given syncs
    every tracked channel.
    Returns the number of new videos upserted.
    """
    conn = db.get_connection()
    try:
        query = "SELECT channel_id, genre, source, last_known_video_count FROM channels"
        clauses: list = []
        params: list = []
        if channel_ids is not None:
            placeholders = ",".join("?" for _ in channel_ids)
            clauses.append(f"channel_id IN ({placeholders})")
            params.extend(channel_ids)
        if genres is not None:
            placeholders = ",".join("?" for _ in genres)
            clauses.append(f"genre IN ({placeholders})")
            params.extend(genres)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
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
        youtube = get_client()
        upserted = 0
        processed = 0

        manual_video_siblings: dict[str, list[str]] = {}
        for row in rows:
            if row["source"] == "manual_video":
                manual_video_siblings.setdefault(row["genre"], []).append(row["channel_id"])

        status_channel_ids = [row["channel_id"] for row in rows if row["source"] != "manual_video"]
        try:
            channel_statuses = _prefetch_channel_statuses(youtube, status_channel_ids)
        except api_budget.QuotaExceededError as e:
            print(f"  [catalog] {e}")
            print(f"  [catalog] Stopped before checking any channel - "
                  f"already-synced channels will be skipped on the next `sync` run.")
            return upserted
        except _HTTP_ERRORS as e:
            if _is_quota_error(e):
                print(f"  [catalog] YouTube quota exhausted: {e}")
                print(f"  [catalog] Stopped before checking any channel - "
                      f"already-synced channels will be skipped on the next `sync` run.")
                return upserted
            raise

        for i, row in enumerate(rows, start=1):
            if shutdown.requested():
                print(f"  [catalog] Stopped after {processed}/{total} channel(s) - "
                      f"already-synced channels will be skipped on the next `sync` run.")
                break
            channel_id = row["channel_id"]
            progress = f"[catalog {i}/{total}]"
            try:
                if row["source"] == "manual_video":
                    added = _sync_manual_video_channel(
                        conn, youtube, channel_id, row["genre"], manual_video_siblings[row["genre"]],
                    )
                    _mark_synced(conn, channel_id, None)
                    upserted += added
                    processed += 1
                    print(f"  {progress} {channel_id}: pinned video(s) checked, {added} new upserted")
                    continue

                if channel_id in channel_statuses:
                    playlist_id, video_count = channel_statuses[channel_id]
                else:
                    # This channel's chunk hit a transient, non-quota error
                    # during the upfront batch (see _prefetch_channel_statuses) -
                    # fall back to asking about it alone rather than losing it
                    # for the whole run.
                    playlist_id, video_count = _channel_status(youtube, channel_id)
                if playlist_id is None:
                    print(f"  {progress} {channel_id}: no uploads playlist (channel deleted/private?), skipping")
                    continue

                if video_count == 0:
                    # A channel that has never published anything still
                    # reports an uploads playlist ID, but that playlist
                    # doesn't exist - asking for its items is a 404. Record
                    # the count so the unchanged-channel check below skips
                    # it for one unit next time.
                    print(f"  {progress} {channel_id}: no uploads yet, skipping")
                    _mark_synced(conn, channel_id, video_count)
                    processed += 1
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
                known_ids = {r["video_id"] for r in existing} | _rejected_video_ids(conn, channel_id)

                # Non-empty only for large multi-artist channels (see
                # _SHARED_CHANNEL_VIDEO_THRESHOLD) - those are the only ones
                # where a title's artist isn't already implied by channel_id
                # alone, so this is the only case where song_grouping needs
                # per-title artist tagging to avoid merging two different
                # artists' same-titled songs.
                shared_index = (
                    artist_index_by_genre.get(row["genre"], no_artist_index)
                    if row["source"] != "wikidata" and (video_count or 0) > _SHARED_CHANNEL_VIDEO_THRESHOLD
                    else no_artist_index
                )

                new_uploads = _list_new_uploads(youtube, playlist_id, known_ids)
                new_mvs = []
                rejected = []
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
                    blocked = [v for v in new_uploads if is_blocked_title(v["title"])]
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
                    new_mv_ids = {v["video_id"] for v in new_mvs}
                    duration_rejected = [v for v in candidates if v["video_id"] not in new_mv_ids]

                    # Never a song (title didn't name the song as the focal
                    # point, or duration fell outside the MV window) - kept
                    # out of `videos` entirely (see rejected_videos' schema
                    # comment) so song_id NULL there means only one thing:
                    # a video decouple.py cleared and regroup.py still needs
                    # to (re-)group. Recorded here anyway, in rejected_videos
                    # rather than dropped, so a channel that regularly posts
                    # this kind of content doesn't have every future sync
                    # re-walk past (and re-fetch duration for) the same
                    # already-rejected uploads indefinitely.
                    rejected = blocked + duration_rejected

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

                rejected_video_dicts = [
                    {
                        "video_id":   v["video_id"],
                        "channel_id": channel_id,
                        "title":      v["title"],
                        "checked_at": datetime.now().isoformat(),
                    }
                    for v in rejected
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
                if rejected_video_dicts:
                    conn.executemany(
                        """
                        INSERT INTO rejected_videos (video_id, channel_id, title, checked_at)
                        VALUES (:video_id, :channel_id, :title, :checked_at)
                        ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, checked_at=excluded.checked_at
                        """,
                        rejected_video_dicts,
                    )
                conn.execute(
                    "UPDATE channels SET last_catalog_sync = ?, last_known_video_count = ? WHERE channel_id = ?",
                    (datetime.now().isoformat(), video_count, channel_id),
                )
                conn.commit()
                upserted += len(new_mvs)
                processed += 1
                rejected_note = f", {len(rejected_video_dicts)} rejected (not a song)" if rejected_video_dicts else ""
                print(f"    done - {len(new_mvs)} new video(s) upserted{rejected_note}")

            except api_budget.QuotaExceededError as e:
                print(f"  [catalog] {e}")
                print(f"  [catalog] Stopped after {processed}/{total} channel(s) - "
                      f"already-synced channels will be skipped on the next `sync` run.")
                break

            except _HTTP_ERRORS as e:
                # One channel failing must not end the run. A roster of
                # several hundred always contains a few that are deleted,
                # private, region-blocked, or whose uploads playlist has
                # gone missing - and a bootstrap that aborts on the first
                # of them never reaches the rest.
                if _is_quota_error(e):
                    print(f"  [catalog] YouTube quota exhausted: {e}")
                    print(f"  [catalog] Stopped after {processed}/{total} channel(s) - "
                          f"already-synced channels will be skipped on the next `sync` run.")
                    break

                status = getattr(getattr(e, "resp", None), "status", "?")
                print(f"  {progress} {channel_id}: YouTube returned {status}, skipping this channel")
                # Marked synced so a permanently broken channel isn't
                # re-attempted on every future run. A channel that starts
                # working again changes its video count, which brings it
                # back through the normal path.
                _mark_synced(conn, channel_id, row["last_known_video_count"])
                continue

        return upserted
    finally:
        conn.close()
