"""
Shared filter for identifying official music videos among search/catalog
results. Used by search.py, youtube_api.py, and catalog.py so the
compilation/duration heuristics live in exactly one place.
"""

import re

# Titles matching this pattern are almost certainly compilations, playlists,
# or aggregator videos rather than individual official MVs.
BLOCKLIST = re.compile(
    r"\b("
    r"compilation|playlist|mixtape|medley"
    r"|top\s*\d+"
    r"|best\s+of"
    r"|all\s+songs?"
    r"|full\s+album"
    r"|greatest\s+hits"
    r"|mash.?up"
    r"|collection"
    r"|ranking"
    r"|mix"           # "kpop mix 2024" - distinct from "remix" (no word boundary match)
    r"|teaser"        # pre-release promo clip, not the song itself - excluded even when
                       # it clears MIN_DURATION (some run well past a typical teaser length)

    # Promotional and behind-the-scenes uploads. Label channels post these
    # constantly and many run well past MIN_DURATION, so the duration
    # window alone doesn't catch them - on a real registry they accounted
    # for 9% of everything catalogued as a song, each one charting as its
    # own entry. song_grouping's prompt already refuses to merge them into
    # the song they promote, but that only makes them singletons; the
    # decision that they aren't songs at all belongs here.
    r"|trailer|preview|documentary|recap|interview|spoiler|unboxing|vlog|reaction"
    r"|behind[\s-]the[\s-]scenes?"
    r"|making\s+(?:film|of)"
    r"|concept\s+(?:film|photo|clip|trailer)"
    r"|episode|ep\.\s*\d"

    # "Behind" and "making" as standalone video-type markers, distinct from
    # the phrase forms above. A manual audit of ~15,000 already-catalogued
    # titles found the bare-phrase rule above only catches "behind the
    # scenes" and "making film/of" literally, missing every other shape a
    # label channel actually uses: "MV Shooting Behind", "Stage Behind",
    # "Live Clip Behind", "VCR Behind", "MV Making Video", the Korean
    # equivalents (never had a pattern here at all), etc. - roughly 750
    # videos across the registry, none of them a song. Anchored to a
    # production/format noun (mv, stage, clip, practice, vcr, concert,
    # shooting, bonus) rather than a bare "\bbehind\b", because a bare
    # match is unsafe: real song titles contain "Behind" as an ordinary
    # word ("Behind U", "Behind You", "Hands Behind My Back", "Behind The
    # Shine", "Behind the page" are all genuine OST/single titles, not
    # behind-the-scenes footage) and a bare match would silently drop
    # those. If a future audit finds another unanchored shape slipping
    # through, add it here rather than loosening the anchor. The filler
    # between anchor and "behind" allows quote marks too (real titles wrap
    # the anchor in quotes, e.g. "'Live Clip' Behind"), not just bare words.
    r"|(?:mv|m/v|music\s+video|lyric\s+video|stage|clip|practice|vcr|concert|shooting|bonus|encore|performance(?:\s+video)?)"
    r"[\s'\"]*(?:[\w'\"]+[\s'\"]*){0,2}?behind\b"
    r"|behind\s+(?:the\s+)?(?:[\w']+\s+){0,2}?(?:stage|cut)\b"
    r"|mv\s*making|m/v\s*making|making\s+video|making\s+movie"
    r"|shooting\s+sketch|shoot\s+sketch"
    r"|비하인드|메이킹|촬영\s*(?:현장|비하인드)|제작\s*현장"

    # Phrase-anchored on purpose: "Highlight" is itself a K-pop group, so a
    # bare word match here would reject that artist's entire catalogue.
    # Same reasoning keeps "making" tied to film/of rather than standing
    # alone, which would catch a song like "Making Love".
    r"|highlight\s+(?:clip|medley|video|reel)"
    r")\b",
    re.IGNORECASE,
)

# Official MV duration window: 90 s (short singles) -> 720 s (extended cuts).
# Anything shorter is a teaser/clip; longer is a live set or compilation.
MIN_DURATION = 90
MAX_DURATION = 720


# Upload types where the song itself is the content, rather than something
# about the song. A title must name one of these to be catalogued.
#
# This is an allowlist, which is a deliberate choice of precision over
# recall. A blocklist can only reject what it has been taught to name, and
# label channels invent new promotional formats constantly - measured on a
# real registry, titles carrying no recognised type marker at all were
# majority non-song content (vlogs, photo-shoot sketches, album intros,
# variety segments, concert clips), not plain music videos.
#
# The cost is real and worth knowing: a genuine upload that announces no
# type is now rejected too, which loses bare "Artist - Song" titles and
# numbered album-track uploads like "01. REBEL HEART". Roughly half of
# short unmarked titles were of that kind. Widening this tuple is the
# supported way to take some of them back.
ALLOWED_TYPES = re.compile(
    r"("
    r"\bm\s*/\s*v\b|\bmv\b"
    r"|music\s+video|official\s+video"
    r"|dance\s+practice|dance\s+version|choreograph"
    r"|lyric"
    r"|encore"
    # Live formats are included because encore stages are - a single-song
    # live clip has the song as its focal point just as a studio cut does.
    # Full concert films don't slip in behind this: MAX_DURATION rejects
    # them, and recap/highlight edits are named in BLOCKLIST.
    #
    # "live at/from/in ..." catches award-show and broadcast performance
    # titles ("Live at the 68th Annual Grammy Awards", "live from 2024 MAMA
    # AWARDS") that name a venue/event rather than the word "clip" or
    # "performance" itself. Bare "live" alone isn't matched - that would
    # also catch "Livestream" in spirit and titles like "Live Q&A" - but
    # BLOCKLIST already removes the vlog/reaction/etc. shapes that would
    # otherwise slip through this wider net.
    r"|performance|\bstage\b|live\s+clip|live\s+(?:at|from|in)\b"
    r"|visualizer"          # static/looping visual, but the song is the content
    r"|official\s+audio|\baudio\b"
    r")",
    re.IGNORECASE,
)


def is_blocked_title(title: str) -> bool:
    """
    The title-only half of is_valid_mv, split out for callers that pay to
    learn a video's duration. A title rejected here can never pass whatever
    its duration turns out to be, so catalog.py rejects those before
    spending YouTube quota fetching durations it can't use.

    Rejects both promotional formats (BLOCKLIST) and anything that doesn't
    name a type where the song is the focal point (ALLOWED_TYPES).
    """
    if BLOCKLIST.search(title):
        return True
    return not ALLOWED_TYPES.search(title)


def is_valid_mv(title: str, duration: int) -> bool:
    """Return True if the video is likely an official single/MV."""
    if duration < MIN_DURATION or duration > MAX_DURATION:
        return False
    if is_blocked_title(title):
        return False
    return True
