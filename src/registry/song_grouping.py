"""
Groups a channel's videos into songs, so multiple uploads of the same
underlying song (official MV + performance video + dance practice +
lyric video, etc.) aggregate into one chart entry instead of splitting
view counts across separate rows.

Teasers are not part of this - mv_filter's blocklist excludes them before
a video ever reaches catalog sync, since a teaser isn't the song itself
and shouldn't contribute views to it (or exist as its own chart entry).

Additive, not a full recompute: once a video has a song_id it's never
re-classified, so a sync only ever spends work (local or AI) on videos
that don't have one yet.

On most channels a title alone is enough - the channel already implies a
single artist. On large kworb-sourced channels flagged as likely shared
(see catalog.py's _SHARED_CHANNEL_VIDEO_THRESHOLD), that's not true, so
callers pass artist_patterns (Wikidata-confirmed artist name -> regex)
and every tier below folds the title's matched artist into its matching
key/context, so two different artists' identically-titled songs can't
collide into one group.

Three tiers, cheapest first:
  1. Exact match against every already-grouped existing video's
     normalized title - free, no AI, and the common case for a video
     that's an official-cut duplicate of something already tracked.
  2. Local normalization among the remaining new videos (free, no AI) -
     strips predictable video-type suffixes ("Official MV", "Dance
     Practice", ...) and groups same-batch videos whose normalized
     titles match exactly.
  3. Chunked AI pass (Gemini, via gemini_client) for whatever's left -
     stylized titles, alternate romanizations, etc. Given each existing
     song's canonical title as a candidate, so a new upload can link to
     an existing song instead of spawning a duplicate. Falls back to
     leaving videos ungrouped (singletons) on any failure, so a video is
     never silently dropped.
"""

import json
import re
from datetime import datetime

from shared.gemini_client import call_gemini, chunked

_VIDEO_TYPE_KEYWORDS = (
    "official", "mv", "m/v", "lyric", "performance", "practice",
    "choreography", "video", "audio", "visualizer",
)

_BRACKET_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_MV_TOKEN_RE = re.compile(r"\bm\s*/\s*v\b|\bmv\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

_GROUP_PROMPT = """\
You are grouping YouTube video titles that are different uploads of the SAME underlying \
song (e.g. an official MV, a dance practice video, or a lyric/cover upload of the same song \
all count as the same song). Do NOT group different songs together, even by the same artist - \
titles that look similar or near-identical in English/romanization can still be different \
songs. If a title includes Hangul (or other native-script) text, treat that text as the most \
reliable signal of song identity: compare it precisely, character by character, and if two \
titles' native-script text differs at all - including a small added qualifier like a number \
or hanja - they are DIFFERENT songs, never the same song under a different title style. \
Do NOT group promotional/behind-the-scenes content with the song itself, even from the same \
release - album trailers, "making of" videos, photo session / behind-the-scenes footage, and \
similar are NOT the song and must stay in their own singleton group. Only group videos where \
the song itself is the actual content (MV, lyric video, dance practice, performance video, \
acoustic/cover version, audio upload). Some titles are labeled "[artist: X]" - this channel \
hosts multiple artists, so treat titles with DIFFERENT labeled artists as always different \
songs, even if the song title text is identical or near-identical. Unlabeled titles have no \
known artist and should be judged on title text alone.

Songs already tracked for this channel (each has a stable ID). If a new title below is the \
SAME song as one of these, just under a different title style or romanization, use that \
song's ID instead of forming a new group:
{existing}

New titles to classify (numbered):
{entries}

Return ONLY a JSON array - no markdown, no explanation. Each element is an object:
  {{"existing_id": <ID from the tracked list above, or null>, "members": [<numbers>]}}
Use an existing ID only when the group is the same song as one of the tracked songs above; \
use null for a song not in that list. Every number from 1 to {n} must appear in exactly one \
element's members list. A song with only one upload is still its own element. Example:
[{{"existing_id": 12, "members": [1, 3]}}, {{"existing_id": null, "members": [2]}}, \
{{"existing_id": null, "members": [4, 5, 6]}}]"""


def normalize_title(title: str) -> str:
    """
    Reduce a video title to a matching key: drop bracket/paren groups
    that look like video-type markers, drop standalone MV/M-V tokens,
    strip punctuation, collapse whitespace, lowercase. Two videos of the
    same song with predictable "official cut" suffixes normalize to the
    same key with no AI needed.
    """
    def _strip_bracket(match):
        content = match.group(0).lower()
        return "" if any(kw in content for kw in _VIDEO_TYPE_KEYWORDS) else match.group(0)

    text = _BRACKET_RE.sub(_strip_bracket, title)
    text = _MV_TOKEN_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip().lower()
    return text


def _matched_artist(title: str, artist_patterns) -> str | None:
    """First confirmed-artist name whose pattern matches `title`, or None if none do."""
    for name, pattern in artist_patterns:
        if pattern.search(title):
            return name
    return None


def _match_key(title: str, artist_patterns):
    """
    Matching key for `title`: (matched_artist, normalized_title) when
    `artist_patterns` is given (shared/multi-artist channels), else just
    normalized_title - so two different confirmed artists' identically
    titled songs can't collide into the same key. None means the title
    had nothing left after normalization (e.g. an all-bracket title).
    """
    title_key = normalize_title(title)
    if not title_key:
        return None
    return (_matched_artist(title, artist_patterns), title_key) if artist_patterns else title_key


def _local_cluster(videos: list[dict], artist_patterns=()) -> dict:
    groups: dict = {}
    for v in videos:
        key = _match_key(v["title"], artist_patterns) or f"__unmatched__{v['video_id']}"
        groups.setdefault(key, []).append(v)
    return groups


def _ai_group_chunk(
    videos: list[dict], existing_songs: dict[int, str], artist_patterns=(),
) -> list[tuple[int | None, list[dict]]]:
    """
    Cluster one small chunk via Gemini, matching against `existing_songs`
    ({song_id: canonical_title}) so a stylized/alternate-romanization title
    can link to an already-tracked song instead of spawning a duplicate.
    When `artist_patterns` is given (shared/multi-artist channels), every
    title - new and existing - is labeled with its matched confirmed
    artist so the model won't merge two different artists' identically
    titled songs. Returns (existing_song_id_or_None, members) tuples -
    None means a new song not seen before. Falls back to all-new-
    singletons on any failure.
    """
    def _label(title):
        artist = _matched_artist(title, artist_patterns) if artist_patterns else None
        return f"{title} [artist: {artist}]" if artist else title

    existing_list = "\n".join(f"{sid}: {_label(title)}" for sid, title in existing_songs.items()) or "(none tracked yet)"
    entries = "\n".join(f"{i + 1}. {_label(v['title'])}" for i, v in enumerate(videos))
    text = call_gemini(_GROUP_PROMPT.format(existing=existing_list, entries=entries, n=len(videos)))
    if text is None:
        return [(None, [v]) for v in videos]

    try:
        raw_groups = json.loads(text)
        seen = set()
        groups = []
        for raw in raw_groups:
            member_nums = raw.get("members", [])
            members = [videos[i - 1] for i in member_nums if isinstance(i, int) and 1 <= i <= len(videos)]
            seen.update(i for i in member_nums if isinstance(i, int))
            if not members:
                continue
            existing_id = raw.get("existing_id")
            if existing_id not in existing_songs:
                existing_id = None  # hallucinated/omitted ID - treat as a new song instead of crashing
            groups.append((existing_id, members))
        for i, v in enumerate(videos, start=1):
            if i not in seen:
                groups.append((None, [v]))
        return groups
    except (json.JSONDecodeError, ValueError, TypeError, IndexError, AttributeError) as e:
        print(f"  [song_grouping] AI grouping failed ({e}) - leaving this chunk ungrouped.")
        return [(None, [v]) for v in videos]


def group_channel_videos(
    conn, channel_id: str, existing_videos: list, new_videos: list[dict], artist_patterns=(),
) -> dict[str, int]:
    """
    Assign each of `new_videos` a song_id, additively - `existing_videos`'
    song_id assignments (each a row with at least video_id/title/song_id)
    are never touched or recomputed, so AI is only ever spent on videos
    that haven't been resolved before. `artist_patterns` (Wikidata-
    confirmed artist name -> regex) should only be passed for channels
    known to host more than one artist - see module docstring - and folds
    the matched artist into every tier's matching key/context so two
    different artists' identically titled songs can't collide. Returns
    {video_id: song_id} for new_videos only.
    """
    if not new_videos:
        return {}

    existing_by_key: dict = {}
    for v in existing_videos:
        song_id = v["song_id"]
        if song_id is None:
            continue
        key = _match_key(v["title"], artist_patterns)
        if key is not None:
            existing_by_key.setdefault(key, song_id)

    video_song_map: dict[str, int] = {}
    unresolved = []
    for v in new_videos:
        key = _match_key(v["title"], artist_patterns)
        song_id = existing_by_key.get(key) if key is not None else None
        if song_id is not None:
            video_song_map[v["video_id"]] = song_id
        else:
            unresolved.append(v)

    if not unresolved:
        return video_song_map

    # Videos within this batch of unresolved new uploads that exact-match
    # each other (title, and artist when artist_patterns applies) are
    # assumed to be a brand new song without an AI check against existing
    # songs - the per-video lookup above already ruled out an exact match
    # against anything existing, and a same-batch upload coincidentally
    # exact-normalizing to an existing song under a different title style
    # is rare enough to accept.
    local_groups = _local_cluster(unresolved, artist_patterns)
    confident = [members for members in local_groups.values() if len(members) > 1]
    singles    = [members[0] for members in local_groups.values() if len(members) == 1]

    existing_songs = {
        row["song_id"]: row["canonical_title"]
        for row in conn.execute("SELECT song_id, canonical_title FROM songs WHERE channel_id = ?", (channel_id,))
    }

    chunks = chunked(singles)
    if len(chunks) > 2:
        # A large channel can mean dozens of chunked Gemini calls in a
        # row - without this, that stretch looks identical to a hang.
        print(f"    {len(singles)} title(s) need AI grouping ({len(chunks)} chunk(s))...")

    resolved: list[tuple[int | None, list[dict]]] = [(None, members) for members in confident]
    for i, chunk in enumerate(chunks, start=1):
        if len(chunks) > 2 and i % 5 == 0:
            print(f"      ...chunk {i}/{len(chunks)}")
        resolved.extend(_ai_group_chunk(chunk, existing_songs, artist_patterns))

    now = datetime.now().isoformat()
    for existing_id, members in resolved:
        if existing_id is not None:
            for v in members:
                video_song_map[v["video_id"]] = existing_id
            continue
        canonical_title = min(members, key=lambda v: len(v["title"]))["title"]
        cursor = conn.execute(
            "INSERT INTO songs (channel_id, canonical_title, grouped_at) VALUES (?, ?, ?)",
            (channel_id, canonical_title, now),
        )
        for v in members:
            video_song_map[v["video_id"]] = cursor.lastrowid

    return video_song_map
