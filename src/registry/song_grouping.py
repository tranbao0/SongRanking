"""
Groups a channel's videos into songs, so multiple uploads of the same
underlying song (official MV + performance video + dance practice +
lyric video, etc.) aggregate into one chart entry instead of splitting
view counts across separate rows.

Teasers are not part of this - mv_filter's blocklist excludes them before
a video ever reaches catalog sync, since a teaser isn't the song itself
and shouldn't contribute views to it (or exist as its own chart entry).

Two stages, to keep AI usage small and focused rather than sending one
big list per channel:
  1. Local normalization (free, no AI) - strips predictable video-type
     suffixes ("Official MV", "Dance Practice", ...) and groups videos
     whose normalized titles match exactly. Covers the common case.
  2. Chunked AI pass (Gemini, via gemini_client) for whatever didn't get
     an exact match - stylized titles, alternate romanizations, etc.
     Falls back to leaving videos ungrouped (singletons) on any failure,
     so a video is never silently dropped.
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
all count as the same song). Do NOT group different songs together, even by the same artist. \
Do NOT group promotional/behind-the-scenes content with the song itself, even from the same \
release - album trailers, "making of" videos, photo session / behind-the-scenes footage, and \
similar are NOT the song and must stay in their own singleton group. Only group videos where \
the song itself is the actual content (MV, lyric video, dance practice, performance video, \
acoustic/cover version, audio upload).

Titles (numbered):
{entries}

Return ONLY a JSON array of groups - no markdown, no explanation. Each group is a list of \
the numbers (as integers) that are the same song. Every number from 1 to {n} must appear in \
exactly one group. A song with only one upload is still its own group of size 1. Example: \
[[1, 3], [2], [4, 5, 6]]"""


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


def _local_cluster(videos: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for v in videos:
        key = normalize_title(v["title"]) or f"__unmatched__{v['video_id']}"
        groups.setdefault(key, []).append(v)
    return groups


def _ai_group_chunk(videos: list[dict]) -> list[list[dict]]:
    """Cluster one small chunk via Gemini; falls back to all-singletons on failure."""
    entries = "\n".join(f"{i + 1}. {v['title']}" for i, v in enumerate(videos))
    text = call_gemini(_GROUP_PROMPT.format(entries=entries, n=len(videos)))
    if text is None:
        return [[v] for v in videos]

    try:
        raw_groups = json.loads(text)
        seen = set()
        groups = []
        for raw in raw_groups:
            members = [videos[i - 1] for i in raw if isinstance(i, int) and 1 <= i <= len(videos)]
            seen.update(i for i in raw if isinstance(i, int))
            if members:
                groups.append(members)
        for i, v in enumerate(videos, start=1):
            if i not in seen:
                groups.append([v])
        return groups
    except (json.JSONDecodeError, ValueError, TypeError, IndexError) as e:
        print(f"  [song_grouping] AI grouping failed ({e}) - leaving this chunk ungrouped.")
        return [[v] for v in videos]


def group_channel_videos(conn, channel_id: str, videos: list[dict]) -> dict[str, int]:
    """
    Cluster `videos` (each a dict with at least video_id/title) into
    songs for `channel_id`, overwriting that channel's songs rows, and
    return {video_id: song_id}. Full recompute each call rather than
    incremental merging - simpler and self-correcting as new uploads
    appear than trying to reconcile against a prior grouping.
    """
    if not videos:
        return {}

    local_groups = _local_cluster(videos)
    confident = [members for members in local_groups.values() if len(members) > 1]
    singles    = [members[0] for members in local_groups.values() if len(members) == 1]

    chunks = chunked(singles)
    if len(chunks) > 2:
        # A large channel can mean dozens of chunked Gemini calls in a
        # row - without this, that stretch looks identical to a hang.
        print(f"    {len(singles)} title(s) need AI grouping ({len(chunks)} chunk(s))...")

    final_groups = list(confident)
    for i, chunk in enumerate(chunks, start=1):
        if len(chunks) > 2 and i % 5 == 0:
            print(f"      ...chunk {i}/{len(chunks)}")
        final_groups.extend(_ai_group_chunk(chunk))

    conn.execute("DELETE FROM songs WHERE channel_id = ?", (channel_id,))

    now = datetime.now().isoformat()
    video_song_map: dict[str, int] = {}
    for group in final_groups:
        canonical_title = min(group, key=lambda v: len(v["title"]))["title"]
        cursor = conn.execute(
            "INSERT INTO songs (channel_id, canonical_title, grouped_at) VALUES (?, ?, ?)",
            (channel_id, canonical_title, now),
        )
        for v in group:
            video_song_map[v["video_id"]] = cursor.lastrowid
    return video_song_map
