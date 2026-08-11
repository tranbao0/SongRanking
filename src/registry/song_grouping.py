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
single artist. On large curated channels flagged as likely shared
(see catalog.py's _SHARED_CHANNEL_VIDEO_THRESHOLD), that's not true, so
callers pass artist_patterns (Wikidata-confirmed artist name -> regex)
and every tier below folds the title's matched artist into its matching
key/context, so two different artists' identically-titled songs can't
collide into one group.

Grouping is channel-scoped by default (existing_videos/new_videos are
whatever the caller passes in), but callers can merge in another
channel's existing videos too - e.g. catalog.py does this when a shared
channel's video matches a confirmed artist that already has its own
tracked channel - so the same song uploaded to two different channels
(an artist's own channel and a broadcast/stage channel re-uploading
them, for example) still resolves to one song_id instead of two. A
newly created song is anchored to that artist's own channel when
artist_home_channels resolves one, so a later upload on a third channel
can still find it.

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
     an existing song instead of spawning a duplicate. On transport or
     parse failure the chunk's videos are left with song_id NULL (omitted
     from the return map) so `regroup` can finish them later - minting
     singletons here would lock them permanently, since grouping is
     additive and never revisits an assigned song_id.

A re-arranged version of a song - remix, acoustic, instrumental, vocal,
karaoke, a cappella, sped-up/slowed - counts as the SAME song here, not
a different one: it merges into the original's entry, same as an
official MV and its dance practice would. A different-language
re-recording ("(Japanese Ver.)", "(English Ver.)", ...) merges the same
way. (Operator-directed reversal, 2026-08-10, of the opposite rule this
project ran under through the 2026-08-06 - 2026-08-10 whole-registry
audit - see docs/manual-grouping-prompt.md's Precedents section for that
history.)

Chunks in tier 3 are independent of one another: each is given the same
snapshot of existing songs, and no song row is written until all of them
have returned. They are therefore run concurrently, which changes only
how long the pass takes, not what it decides. This also means two
uploads of one song can only be merged if they land in the same chunk -
the reason CHUNK_SIZE is set from measured grouping accuracy rather than
kept small (see gemini_client).
"""

import concurrent.futures
import json
import re
import unicodedata
from datetime import datetime

from shared import gemini_client
from shared.gemini_client import call_gemini, chunked

# Chunks are independent (see group_channel_videos), so these overlap
# purely to hide each other's latency - a grouping call is ~10s of mostly
# waiting. Kept at 2 so a free-tier Flash RPM (often ~5-15/min) is less
# likely to trip; gemini_client retries a 429, but not tripping one is
# cheaper than recovering from it. Raise toward 4 on a paid tier with
# headroom if a first sync feels hung on AI waits.
_AI_CHUNK_WORKERS = 2

_VIDEO_TYPE_KEYWORDS = (
    "official", "mv", "m/v", "lyric", "performance", "practice",
    "choreography", "video", "audio", "visualizer", "live",
)

# Camera-angle/take qualifiers on a Dance Practice or Performance Video -
# "(Moving Cam ver.)", "(Fix ver.)", "(Close-up ver.)" - name which camera
# recorded the same performance, not a different arrangement or a producer/
# occasion credit, so per the doc's decided rules these always merge with
# the plain upload. None of these words double as a producer name, remix
# credit, or language/occasion tag in this project's data (unlike a bare
# "ver"/"version", which does - e.g. "(Steve Aoki Ver.)", "(Japanese Ver.)"
# - so this list is intentionally narrower than _VIDEO_TYPE_KEYWORDS and
# matched with \b rather than substring-in-content, to avoid "cam" matching
# inside an unrelated word (a producer surname, say). Checked separately
# from _VIDEO_TYPE_KEYWORDS because a bracket containing only one of these
# words and no other video-type keyword - "(Moving Cam ver.)" contains
# neither "video" nor "performance" - previously survived normalization
# untouched and silently blocked tier 1/2 from linking a Dance Practice
# upload to its own song (VIXX's "향 (Scentist)" Dance Practice Video
# (Moving Cam ver.)" never matched its own Official M/V, found in a
# 2026-08-05 whole-registry audit).
_CAMERA_ANGLE_RE = re.compile(
    r"\b(moving|fix(?:ed)?|cam|close-?up|onetake|one-take|front-?cam|vertical|horizontal)\b",
    re.IGNORECASE,
)

# A "(...Ver.)" tag always merges with the plain upload, regardless of
# what the tag says or what kind of video it's attached to - a costume
# theme ("Halloween ver."), a camera angle, an occasion, a producer/remix
# alias ("Steve Aoki Ver."), or a language ("Japanese Ver.") are all
# presentational from this project's perspective as of the 2026-08-10
# rule reversal (see the module docstring): a remix or a different-
# language re-recording is another version of the SAME song now, not a
# different one, so there's no longer a category of Ver. tag that should
# block a merge. (Previously this was gated to a Dance/Performance/
# Choreography video and excluded language names - see git history if
# that narrower rule is ever needed again.)
_VER_TAG_RE = re.compile(r"\bver(?:sion)?\.?\b", re.IGNORECASE)

# A rearrangement/mix marker - "(Remix)", a named producer/DJ remix
# ("(Sara Landry Remix)", matched via the word "remix"), "(Acoustic
# Ver.)", "(Instrumental)"/"(Inst.)", "(Vocal Ver.)", "(Karaoke Ver.)",
# "(A Cappella)"/"(Acapella)", "(Sped Up Ver.)", "(Slowed)" - merges with
# the plain upload (2026-08-10 rule reversal: these used to be the
# single most common DO-NOT-merge case in this data; now they're the
# same song, presented as a different mix). Matched with \b, not
# substring-in-content like _VIDEO_TYPE_KEYWORDS, so "inst" doesn't
# false-positive inside an unrelated word.
_ARRANGEMENT_RE = re.compile(
    r"\b(remix|acoustic|instrumental|inst\.?|vocal|karaoke|a\s*cappella|acapella|"
    r"sped[\s-]?up|slowed)\b",
    re.IGNORECASE,
)

# A language name, bracketed with or without a "Ver." tag - "(Japanese
# Ver.)", "(Japanese)", "(Chinese Ver.)" - merges with the plain upload.
# A different-language re-recording is a different vocal take of the
# same song, but per the 2026-08-10 rule reversal that now counts as
# another version of the SAME song, same as a remix or an acoustic take.
_LANGUAGE_RE = re.compile(
    r"\b(korean|japanese|chinese|english|mandarin|cantonese|thai|spanish|french|german)\b",
    re.IGNORECASE,
)

_BRACKET_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_MV_TOKEN_RE = re.compile(r"\bm\s*/\s*v\b|\bmv\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# Upload-type markers as they appear *unbracketed* at the end of a title -
# "BTS (방탄소년단) 'Dynamite' Official MV", which is the house style on most
# label channels. Bracketed markers are already handled above; without
# these, the two commonest forms of the same song ("... Official MV" and
# "... Dance Practice") normalise differently and only the AI tier can
# ever link them, which it can only do when both land in the same chunk.
#
# Matched as whole phrases rather than individual words, and only at the
# end of the title: "Last Dance" and "Video Games" are real song titles,
# and stripping a bare trailing "dance" or "video" would truncate them
# into something that could collide with a genuinely different song.
_UPLOAD_TYPE_MARKERS = [
    "official music video", "official lyric video", "official performance video",
    "official dance practice", "special performance video", "dance performance video",
    "choreography version", "choreography video", "performance version", "official video",
    "official audio", "official mv", "music video", "lyric video", "lyrics video",
    "dance practice video", "choreography practice video", "choreography practice",
    "dance practice", "dance version", "performance video", "practice video",
    "band version", "visualizer", "official", "audio", "instrumental",
    "remix", "acoustic", "vocal", "karaoke", "acapella", "a cappella",
    "sped up", "slowed",
]
_TRAILING_MARKER_RE = re.compile(
    r"(?:\s+(?:" + "|".join(sorted((re.escape(m) for m in _UPLOAD_TYPE_MARKERS), key=len, reverse=True)) + r"))+$"
)

_GROUP_PROMPT = """\
You are grouping YouTube video titles that are different uploads of the SAME underlying \
song (e.g. an official MV, a dance practice video, or a lyric upload of the same song \
all count as the same song). Do NOT group different songs together, even by the same artist - \
titles that look similar or near-identical in English/romanization can still be different \
songs. If a title includes Hangul (or other native-script) text, treat that text as the most \
reliable signal of song identity: compare it precisely, character by character, and if two \
titles' native-script text differs at all - including a small added qualifier like a number \
or hanja - they are DIFFERENT songs, never the same song under a different title style. \
EXCEPTION to that rule: a title explicitly marked as a different-language version of the same \
song - a "(Japanese Ver.)", "(English Ver.)", "(Chinese Ver.)"-style tag, or an explicit \
translation of the same song name - is the SAME song as the original-language version and \
should be grouped with it, even though its native-script text is a different script entirely, \
not just a differing character. Recognize a language-version pair from the explicit tag or \
translation, never by comparing native-script text - that comparison is only for deciding \
whether two same-language titles are the same or different songs. \
Do NOT group promotional/behind-the-scenes content with the song itself, even from the same \
release - album trailers, "making of"/"making video"/메이킹 videos, "MV Behind"/"Stage Behind"/\
"Shooting Sketch"/비하인드/촬영 비하인드 footage, and similar are NOT the song and must stay in \
their own singleton group, never merged into the real song's group even when the surrounding \
title is otherwise identical. A re-arranged version of a song - a remix (including a named \
producer/DJ remix like "(Sara Landry Remix)"), an acoustic, instrumental, vocal, karaoke, or a \
cappella version, or a sped-up/slowed version - is the SAME song and should be grouped with \
the original, the same way an MV and its dance practice would be, even when the title carries \
a "(Remix)", "(Acoustic Ver.)", "(Sped Up Ver.)" or similar tag naming the rearrangement. Group \
videos that are the same song, whether presented as the same recording in a different format \
(MV, lyric video, dance practice, performance video, plain audio upload) or as a different \
arrangement/mix of the same song (remix, acoustic, instrumental, sped-up/slowed) or a \
different-language recording of the same song. Some titles are labeled "[artist: X]" - this channel \
hosts multiple artists, so treat titles with DIFFERENT labeled artists as always different \
songs, even if the song title text is identical or near-identical. Unlabeled titles have no \
known artist and should be judged on title text alone. Some titles are also labeled \
"[tags: ...]" with the uploader's own YouTube tags - use these only as a supporting hint (e.g. \
a tag naming the song in a different script or romanization than the title uses), never to \
override what the title itself says: two titles whose native-script text differs are still \
different songs even if their tags overlap (unless the language-version exception above \
applies), and shared generic tags (the artist's name, "kpop", "official") mean nothing on \
their own.

Songs already tracked for this channel (each has a stable ID). If a new title below is the \
SAME song as one of these, just under a different title style or romanization, use that \
song's ID instead of forming a new group:
{existing}

New titles to classify (numbered 1 to {n}):
{entries}

For each group set existing_id to a tracked song ID when the group is that same song, or \
null when it is a song not in the tracked list. Put every number from 1 to {n} in exactly \
one group's members list. A song with only one upload is still its own group."""

# Enforced by the Interactions API via response_format (see call_gemini), so
# the prompt no longer spends tokens teaching JSON shape / banning markdown.
# existing_id is integer|null; members are 1-based indexes into the chunk.
_GROUP_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "existing_id": {"type": ["integer", "null"]},
            "members": {
                "type": "array",
                "items": {"type": "integer"},
            },
        },
        "required": ["existing_id", "members"],
    },
}


def normalize_title(title: str) -> str:
    """
    Reduce a video title to a matching key: drop bracket/paren groups
    that look like video-type markers, drop standalone MV/M-V tokens,
    strip punctuation, collapse whitespace, lowercase, then drop any
    trailing unbracketed upload-type phrase. Two videos of the same song
    with predictable "official cut" suffixes normalize to the same key
    with no AI needed - which is the only tier that can link them
    reliably, since the AI tier only ever sees one chunk at a time.

    A title consisting of nothing but markers keeps its normalized form
    rather than collapsing to empty, so a song genuinely called e.g.
    "Audio" still gets a usable key instead of being treated as untitled.

    NFC-normalized first: YouTube metadata occasionally arrives with Hangul
    stored as NFD (decomposed Jamo) rather than NFC (precomposed syllables) -
    visually identical, but a different code point sequence, so two uploads
    of the same song in different normalization forms would otherwise never
    match here even though every other character in the title is identical.
    A 2026-08-05 whole-registry audit found 138 existing video titles stored
    as NFD.
    """
    def _strip_bracket(match):
        content = match.group(0).lower()
        is_marker = (
            any(kw in content for kw in _VIDEO_TYPE_KEYWORDS)
            or _CAMERA_ANGLE_RE.search(content)
            or _VER_TAG_RE.search(content)
            or _ARRANGEMENT_RE.search(content)
            or _LANGUAGE_RE.search(content)
        )
        return "" if is_marker else match.group(0)

    text = unicodedata.normalize("NFC", title)
    text = _BRACKET_RE.sub(_strip_bracket, text)
    text = _MV_TOKEN_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip().lower()
    return _TRAILING_MARKER_RE.sub("", text).strip() or text


# An artist name matches when it appears in a title without being glued to
# a longer word. \b can't express that on its own: it marks a word/non-word
# transition, so for a name whose first or last character isn't a word
# character there is no transition to find. r"\bMAMAMOO\+\b" never matches
# "MAMAMOO+ - HIP", because the position after "+" has a non-word character
# on both sides. Eight of the Wikidata-confirmed kpop artists are shaped
# that way - f(x), MAMAMOO+, 100%, 15&, MAVE:, ICHILLIN' among them - and a
# name that can never match means that artist's uploads are dropped from a
# shared channel's catalogue entirely rather than merely left untagged.
#
# Lookarounds state the intent directly, and are exactly equivalent to \b
# for every name that does start and end with a word character.
_ARTIST_PREFIX = r"(?<!\w)"
_ARTIST_SUFFIX = r"(?!\w)"


def artist_pattern(name: str) -> re.Pattern:
    """
    Compile a confirmed-artist name into its title-matching regex. The
    single source of truth for that anchoring - _ArtistIndex builds its
    combined prefilter from the same anchors, and a prefilter anchored
    differently from the individual patterns would reject titles those
    patterns would have matched.
    """
    return re.compile(_ARTIST_PREFIX + re.escape(name) + _ARTIST_SUFFIX, re.IGNORECASE)


class _ArtistIndex:
    """
    Confirmed-artist (name, regex) pairs plus the two things that make
    matching them affordable at catalogue scale, where the list is one
    entry per Wikidata-confirmed artist in the genre (~630 for kpop):

      - a single combined alternation, used only as a prefilter. A title
        containing no confirmed artist at all - the common case on a
        shared channel's non-genre uploads - is rejected in one pass
        instead of one pass per artist.
      - a per-title memo. The same title is matched repeatedly within a
        sync (catalogue filter, cross-channel lookup, match key, local
        cluster, AI prompt label, new-song anchor) and the answer can't
        change mid-run.

    Match order is deliberately unchanged: on a title that does contain a
    confirmed artist, the ordered per-pattern scan below still decides
    which one wins, so collaboration titles naming two confirmed artists
    resolve exactly as they did before. The prefilter only ever answers
    "no confirmed artist here", which it can do without affecting that.
    """

    def __init__(self, patterns):
        self._patterns = list(patterns)
        self._memo: dict[str, str | None] = {}
        self._prefilter = (
            re.compile(
                _ARTIST_PREFIX + "(?:" + "|".join(re.escape(n) for n, _ in self._patterns) + ")" + _ARTIST_SUFFIX,
                re.IGNORECASE,
            )
            if self._patterns else None
        )

    def __bool__(self) -> bool:
        """False for the no-confirmed-artists case, so `if artist_patterns:` still reads naturally."""
        return bool(self._patterns)

    def match(self, title: str) -> str | None:
        """First confirmed-artist name whose pattern matches `title`, or None if none do."""
        if not self._patterns:
            return None
        try:
            return self._memo[title]
        except KeyError:
            pass
        result = None
        if self._prefilter.search(title):
            for name, pattern in self._patterns:
                if pattern.search(title):
                    result = name
                    break
        self._memo[title] = result
        return result


def build_artist_index(patterns) -> _ArtistIndex:
    """
    Wrap (artist_name, regex) pairs for matching. Build one per genre and
    reuse it - compiling the combined prefilter isn't free, and sharing an
    instance also shares its per-title memo across channels.
    """
    return patterns if isinstance(patterns, _ArtistIndex) else _ArtistIndex(patterns)


def _matched_artist(title: str, artist_patterns) -> str | None:
    """First confirmed-artist name whose pattern matches `title`, or None if none do."""
    return build_artist_index(artist_patterns).match(title)


def _match_key(title: str, artist_index: _ArtistIndex):
    """
    Matching key for `title`: (matched_artist, normalized_title) when
    `artist_index` is non-empty (shared/multi-artist channels), else just
    normalized_title - so two different confirmed artists' identically
    titled songs can't collide into the same key. None means the title
    had nothing left after normalization (e.g. an all-bracket title).
    """
    title_key = normalize_title(title)
    if not title_key:
        return None
    return (artist_index.match(title), title_key) if artist_index else title_key


def _local_cluster(videos: list[dict], artist_index: _ArtistIndex) -> dict:
    groups: dict = {}
    for v in videos:
        key = _match_key(v["title"], artist_index) or f"__unmatched__{v['video_id']}"
        groups.setdefault(key, []).append(v)
    return groups


_MALFORMED_RESPONSE_ERRORS = (
    json.JSONDecodeError, ValueError, TypeError, IndexError, AttributeError,
)

# How many times a chunk is asked to classify before giving up and leaving
# it ungrouped. Applies only to a response that arrives but can't be read -
# call_gemini already retries the transport itself, so a None from it is
# a failure that has exhausted its own retries and won't improve here.
#
# Kept at 1 now that response_format enforces JSON shape at the API: a
# parse failure under structured outputs is rare and almost never fixed by
# re-asking, and each re-ask is a full billed request against a permanent-
# singleton failure mode we no longer need to insure this way.
_MAX_PARSE_ATTEMPTS = 1


def _row_get(row, key, default=None):
    """Read an optional column from a sqlite3.Row or a plain dict."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


# Caps on what reaches the AI prompt, not on what's stored - catalog.py
# persists the uploader's full tag list. A handful of channels tag videos
# with dozens of keywords; capped here so one chunky video doesn't blow up
# a chunk's prompt size, since tags are only ever a supporting hint (see
# _GROUP_PROMPT) and diminishing tags past the first few rarely add signal.
_MAX_TAGS_IN_PROMPT = 8
_MAX_TAG_LEN = 40


def _video_tags_label(v) -> str:
    """
    Comma-joined tags for one video, ready to drop into an AI-tier prompt
    label - "" (meaning: omit the "[tags: ...]" suffix entirely) for a
    video with no tags, an unparseable tags column, or one catalogued
    before this column existed. `videos.tags` is stored as a JSON-encoded
    list (see catalog.py); tolerant of it already being a list (a video
    dict built fresh in this sync run, not yet round-tripped through the
    database) so callers don't need to care which shape they have.
    """
    raw = _row_get(v, "tags")
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            tags = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    else:
        tags = raw
    if not isinstance(tags, list):
        return ""
    trimmed = [str(t)[:_MAX_TAG_LEN] for t in tags[:_MAX_TAGS_IN_PROMPT] if t]
    return ", ".join(trimmed)


def _scoped_candidates(chunk, existing_songs, song_anchors, artist_index, artist_home_channels, channel_id):
    """
    Narrow the candidate list sent with one chunk to songs a title in that
    chunk could actually be.

    The full list is every song on every channel the caller merged in. On
    a shared channel that means one entry per song of every confirmed
    artist appearing anywhere on it - hundreds of artists, thousands of
    songs - repeated in full in each chunk's prompt, even though a title
    by one artist can never be another artist's song. That is the single
    largest input-token cost in a catalogue sync, and it grows as the
    registry fills.

    Scoping is by artist rather than by text similarity on purpose. A
    title and its match may share no words at all - an alternate
    romanization or native-script rendering is exactly the case tier 3
    exists to catch - so dropping candidates that merely look unrelated
    would defeat the tier. Which artist a song belongs to is instead read
    from its anchor channel, which is exact.

    Returns `existing_songs` unchanged whenever scoping can't be done
    safely: no artist patterns (a single-artist channel's list is already
    small), no anchor information, or a chunk whose titles resolve to no
    confirmed artist at all.
    """
    if not artist_index or not song_anchors:
        return existing_songs

    homes = set()
    for v in chunk:
        artist = artist_index.match(v["title"])
        home = artist_home_channels.get(artist) if artist else None
        if home:
            homes.add(home)
    if not homes:
        return existing_songs

    # Songs anchored here too: this channel's own back catalogue is
    # relevant to anything uploaded to it, whatever the title says.
    homes.add(channel_id)
    scoped = {
        song_id: title
        for song_id, title in existing_songs.items()
        if song_anchors.get(song_id) in homes
    }
    return scoped


def _parse_group_response(
    text: str, videos: list[dict], existing_songs: dict[int, str],
) -> list[tuple[int | None, list[dict]]]:
    """
    Turn one raw model response into (existing_song_id_or_None, members)
    tuples. Tolerant about content - unusable entries are skipped and any
    video the model didn't mention becomes its own group - but raises on
    a response that isn't readable at all, which the caller retries.
    """
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


def _ai_group_chunk(
    videos: list[dict], existing_songs: dict[int, str], artist_index: _ArtistIndex,
    label_existing_artists: bool = True,
) -> list[tuple[int | None, list[dict]]] | None:
    """
    Cluster one small chunk via Gemini, matching against `existing_songs`
    ({song_id: canonical_title}) so a stylized/alternate-romanization title
    can link to an already-tracked song instead of spawning a duplicate.
    When `artist_index` is non-empty (shared/multi-artist channels), new
    titles are labeled with their matched confirmed artist so the model
    won't merge two different artists' identically titled songs.

    `label_existing_artists` adds the same tag on candidate songs. After
    `_scoped_candidates` has already narrowed to one artist's catalogue,
    those tags are redundant and only inflate the repeated prompt prefix -
    pass False in that case. New titles still get artist labels whenever
    `artist_index` is non-empty.

    Candidate songs are emitted in song_id order so parallel chunks that
    share the same scoped catalogue send an identical prefix (better
    implicit-cache hit rate).

    Returns (existing_song_id_or_None, members) tuples - None as
    existing_id means a new song not seen before. Returns None on
    transport/parse failure after retries are spent. The caller must leave
    those videos unassigned (song_id NULL) rather than minting singletons.
    """
    def _label(title, tags="", *, with_artist: bool):
        artist = artist_index.match(title) if with_artist and artist_index else None
        label = f"{title} [artist: {artist}]" if artist else title
        return f"{label} [tags: {tags}]" if tags else label

    # Sorted for a stable cacheable prefix across chunks of the same artist.
    existing_list = "\n".join(
        f"{sid}: {_label(title, with_artist=label_existing_artists)}"
        for sid, title in sorted(existing_songs.items())
    ) or "(none tracked yet)"
    entries = "\n".join(
        f"{i + 1}. {_label(v['title'], _video_tags_label(v), with_artist=True)}"
        for i, v in enumerate(videos)
    )
    prompt = _GROUP_PROMPT.format(existing=existing_list, entries=entries, n=len(videos))

    text = None
    for attempt in range(_MAX_PARSE_ATTEMPTS):
        text = call_gemini(
            prompt,
            response_schema=_GROUP_RESPONSE_SCHEMA,
            thinking_level="low",
        )
        if text is None:
            break  # transport failure, already retried inside call_gemini
        try:
            return _parse_group_response(text, videos, existing_songs)
        except _MALFORMED_RESPONSE_ERRORS as e:
            remaining = _MAX_PARSE_ATTEMPTS - attempt - 1
            if remaining:
                print(f"  [song_grouping] Unreadable AI response ({e}) - re-asking "
                      f"({remaining} attempt(s) left).")
            else:
                print(f"  [song_grouping] AI grouping failed ({e}) - leaving "
                      f"{len(videos)} video(s) with song_id NULL for regroup.")
                return None

    print(f"  [song_grouping] AI call failed - leaving {len(videos)} video(s) "
          f"with song_id NULL for regroup.")
    return None


def group_channel_videos(
    conn, channel_id: str, existing_videos: list, new_videos: list[dict],
    artist_patterns=(), artist_home_channels: dict[str, str] | None = None,
    use_ai: bool = True,
) -> dict[str, int]:
    """
    Assign each of `new_videos` a song_id, additively - `existing_videos`'
    song_id assignments (each a row with at least video_id/title/song_id)
    are never touched or recomputed, so AI is only ever spent on videos
    that haven't been resolved before. `existing_videos` may span more
    than one channel (see module docstring) - matching doesn't care which
    channel a candidate came from, only its title/artist. `artist_patterns`
    (Wikidata-confirmed artist name -> regex) should only be passed for
    channels known to host more than one artist - see module docstring -
    and folds the matched artist into every tier's matching key/context so
    two different artists' identically titled songs can't collide.
    `artist_home_channels` (artist name -> channel_id) anchors a newly
    created song to that artist's own channel instead of `channel_id` when
    resolvable, so a later upload on yet another channel can still find
    it.

    `use_ai` (default True) runs the Gemini tier for titles free tiers
    could not resolve. When False - regroup's mode - only tiers 1 and 2
    run, then leftover singles are still minted as provisional songs
    (so same-run cross-channel tier-1 matching still works) without any
    Gemini call. The caller is expected to hand those provisionals to the
    CLI agent for merge review. Returns {video_id: song_id} for the
    videos this call assigned.
    """
    if not new_videos:
        return {}

    artist_home_channels = artist_home_channels or {}
    # Callers that match artists on more than one channel should build the
    # index once and pass it in (see build_artist_index), so its per-title
    # memo and compiled prefilter are shared rather than rebuilt per call.
    artist_index = build_artist_index(artist_patterns)

    existing_by_key: dict = {}
    for v in existing_videos:
        song_id = v["song_id"]
        if song_id is None:
            continue
        key = _match_key(v["title"], artist_index)
        if key is not None:
            existing_by_key.setdefault(key, song_id)

    video_song_map: dict[str, int] = {}
    unresolved = []
    for v in new_videos:
        key = _match_key(v["title"], artist_index)
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
    local_groups = _local_cluster(unresolved, artist_index)
    confident = [members for members in local_groups.values() if len(members) > 1]
    singles    = [members[0] for members in local_groups.values() if len(members) == 1]

    # Derived from existing_videos rather than a fresh `songs` query, so
    # this automatically covers whatever channels the caller merged in
    # (see module docstring) with no extra query - same shortest-title
    # convention used below when a group becomes a new song.
    existing_songs: dict[int, str] = {}
    song_anchors: dict[int, str] = {}
    for v in existing_videos:
        song_id = v["song_id"]
        if song_id is None:
            continue
        title = v["title"]
        if song_id not in existing_songs or len(title) < len(existing_songs[song_id]):
            existing_songs[song_id] = title
        # Which artist's channel the song belongs to, used to scope each
        # chunk's candidate list (see _scoped_candidates). Absent when a
        # caller supplies rows without it, in which case scoping is
        # skipped rather than guessed at.
        anchor = _row_get(v, "song_channel_id")
        if anchor is not None:
            song_anchors[song_id] = anchor

    # Checked before any chunking work: with no key there is nothing for the
    # AI tier to do, and building prompts only to discard them would cost a
    # pass over every remaining title plus a log line per chunk.
    chunks = (
        chunked(singles) if use_ai and gemini_client.is_available() else []
    )
    if chunks:
        # Even a single chunk is a real network call that can retry with
        # growing backoff on a rate limit - printing only for >2 chunks
        # made that stretch look identical to a silent hang.
        print(f"    {len(singles)} title(s) need AI grouping ({len(chunks)} chunk(s))...")

    resolved: list[tuple[int | None, list[dict]]] = [(None, members) for members in confident]
    if singles and not chunks:
        if use_ai:
            # Gemini unavailable on a sync path: same outcome as a failed
            # call - leave song_id NULL so regroup (free tiers + CLI agent)
            # can finish without a decouple.
            print(f"    {len(singles)} title(s) left with song_id NULL "
                  f"(AI unavailable; run regroup to finish)")
        else:
            # Free-tiers-only (regroup): still mint singletons so later
            # channels in the same run can tier-1 against them (wikidata-
            # first ordering). Gemini is skipped; the CLI agent merges
            # any false splits afterward.
            print(f"    {len(singles)} title(s) minted as provisional songs "
                  f"(free tiers only; CLI agent will review)")
            resolved.extend((None, [v]) for v in singles)

    # Chunks are independent by construction: every one is handed the same
    # existing_songs snapshot, built above and never updated between calls,
    # and no song row is written until all of them have returned. So
    # running them concurrently cannot change which videos end up grouped
    # together - it only stops each ~10s round trip from waiting on the
    # last. Results are consumed in submission order (map, not
    # as_completed) so the song_id each new group is assigned below stays
    # identical to what the sequential version produced.
    # A None result means the chunk failed: those videos stay out of
    # video_song_map (song_id NULL for regroup).
    def _classify(chunk):
        candidates = _scoped_candidates(
            chunk, existing_songs, song_anchors, artist_index, artist_home_channels, channel_id,
        )
        # After artist scoping, every candidate usually shares one home
        # channel - repeating [artist: X] on each line only burns tokens
        # and diversifies the prefix. Keep labels when more than one
        # anchor is still in play (or anchors are unknown).
        anchors_in_play = {song_anchors.get(sid) for sid in candidates}
        anchors_in_play.discard(None)
        label_existing = len(anchors_in_play) != 1
        return _ai_group_chunk(
            chunk, candidates, artist_index,
            label_existing_artists=label_existing,
        )

    def _consume(chunk_groups):
        if chunk_groups is None:
            return
        resolved.extend(chunk_groups)

    if len(chunks) <= 1:
        for chunk in chunks:
            _consume(_classify(chunk))
    else:
        done = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_AI_CHUNK_WORKERS, len(chunks))
        ) as pool:
            for chunk_groups in pool.map(_classify, chunks):
                _consume(chunk_groups)
                done += 1
                if len(chunks) > 2 and done % 5 == 0:
                    print(f"      ...chunk {done}/{len(chunks)}")

    now = datetime.now().isoformat()
    for existing_id, members in resolved:
        if existing_id is not None:
            for v in members:
                video_song_map[v["video_id"]] = existing_id
            continue
        # Anchor to the group's artist's own channel when known, rather
        # than always channel_id, so a later upload of this song on yet
        # another channel can still find it via the caller's cross-channel
        # existing_videos merge (see module docstring).
        artist = artist_index.match(members[0]["title"])
        anchor_channel_id = artist_home_channels.get(artist, channel_id) if artist else channel_id
        canonical_title = min(members, key=lambda v: len(v["title"]))["title"]
        cursor = conn.execute(
            "INSERT INTO songs (channel_id, canonical_title, grouped_at) VALUES (?, ?, ?)",
            (anchor_channel_id, canonical_title, now),
        )
        for v in members:
            video_song_map[v["video_id"]] = cursor.lastrowid

    return video_song_map
