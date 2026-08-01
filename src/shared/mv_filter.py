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
    r")\b",
    re.IGNORECASE,
)

# Official MV duration window: 90 s (short singles) -> 720 s (extended cuts).
# Anything shorter is a teaser/clip; longer is a live set or compilation.
MIN_DURATION = 90
MAX_DURATION = 720


def is_blocked_title(title: str) -> bool:
    """
    The title-only half of is_valid_mv, split out for callers that pay to
    learn a video's duration. A blocklisted title can never pass whatever
    its duration turns out to be, so catalog.py rejects those before
    spending YouTube quota fetching durations it can't use.
    """
    return bool(BLOCKLIST.search(title))


def is_valid_mv(title: str, duration: int) -> bool:
    """Return True if the video is likely an official single/MV."""
    if duration < MIN_DURATION or duration > MAX_DURATION:
        return False
    if is_blocked_title(title):
        return False
    return True
