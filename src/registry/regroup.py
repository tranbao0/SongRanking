"""
Re-derives song groupings for videos already in the `videos` table whose
song_id has been cleared (see decouple.py), without any YouTube API call -
every candidate here was already fetched and MV-filtered on a prior sync,
so this only redoes the *grouping* step, not the catalogue.

catalog.sync_videos can't do this itself: it treats "new" as "video_id not
yet in the table", so a video decouple left in place with song_id NULL
still reads as already-seen and is silently skipped. This module runs the
same per-channel grouping (song_grouping.group_channel_videos), including
the shared-channel artist filtering and cross-channel existing-video merge
catalog.py uses, but sources "new videos" from the DB's NULL-song_id rows
instead of a live playlist walk.

Channels are processed confirmed-artist-first (source = 'wikidata' before
everything else), not by channel_id. Cross-channel matching only looks at
an artist's own channel when that channel's videos already carry a
song_id (song_grouping ignores NULL-song_id rows when building its match
keys) - so a label channel processed before the artist's own channel would
find nothing to link to and mint a duplicate song. Since only shared
channels do cross-channel lookups at all (see catalog.py's
_SHARED_CHANNEL_VIDEO_THRESHOLD), grouping every confirmed artist channel
first gives every later shared-channel lookup the best chance of a hit.

Resumable for free: a channel whose ungrouped videos were all resolved is
absent from the next run's query (nothing left with song_id NULL), so an
interrupted run picks back up without separate bookkeeping.
"""

from registry import db, song_grouping
from registry.catalog import _SHARED_CHANNEL_VIDEO_THRESHOLD, _confirmed_artists, _existing_videos


def _channels_with_ungrouped_videos(conn, genre: str | None):
    query = """
        SELECT DISTINCT c.channel_id, c.genre, c.source, c.last_known_video_count
        FROM channels c
        JOIN videos v ON v.channel_id = c.channel_id
        WHERE v.song_id IS NULL
    """
    params: list = []
    if genre is not None:
        query += " AND c.genre = ?"
        params.append(genre)
    # Wikidata (artist-owned) channels first - see module docstring.
    query += " ORDER BY CASE WHEN c.source = 'wikidata' THEN 0 ELSE 1 END, c.channel_id"
    return conn.execute(query, params).fetchall()


def regroup_all(genre: str | None = None) -> int:
    """
    Group every ungrouped (song_id IS NULL) video, channel by channel.
    Returns the number of videos assigned a song_id.
    """
    conn = db.get_connection()
    try:
        rows = _channels_with_ungrouped_videos(conn, genre)
        total = len(rows)
        print(f"  [regroup] {total} channel(s) with ungrouped video(s)")

        artist_patterns, official_channels = _confirmed_artists(conn)
        artist_index_by_genre = {
            g: song_grouping.build_artist_index(patterns) for g, patterns in artist_patterns.items()
        }
        no_artist_index = song_grouping.build_artist_index([])

        grouped = 0
        for i, row in enumerate(rows, start=1):
            channel_id = row["channel_id"]
            progress = f"[regroup {i}/{total}]"

            new_rows = conn.execute(
                "SELECT video_id, title, url, published_at, discovered_at "
                "FROM videos WHERE channel_id = ? AND song_id IS NULL",
                (channel_id,),
            ).fetchall()
            new_video_dicts = [dict(r) for r in new_rows]
            if not new_video_dicts:
                # Already resolved earlier this run as a cross-channel target.
                continue

            shared_index = (
                artist_index_by_genre.get(row["genre"], no_artist_index)
                if row["source"] != "wikidata"
                and (row["last_known_video_count"] or 0) > _SHARED_CHANNEL_VIDEO_THRESHOLD
                else no_artist_index
            )

            existing = _existing_videos(conn, channel_id)

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

            extra = f" (cross-checking {len(cross_channel_ids)} artist channel(s))" if cross_channel_ids else ""
            print(f"  {progress} {channel_id}: grouping {len(new_video_dicts)} video(s)...{extra}")
            song_map = song_grouping.group_channel_videos(
                conn, channel_id, existing + cross_existing, new_video_dicts,
                artist_patterns=shared_index, artist_home_channels=official_channels,
            )

            conn.executemany(
                "UPDATE videos SET song_id = :song_id WHERE video_id = :video_id",
                [{"video_id": vid, "song_id": sid} for vid, sid in song_map.items()],
            )
            conn.commit()
            grouped += len(song_map)

        print(f"  [regroup] {grouped} video(s) grouped.")
        return grouped
    finally:
        conn.close()
