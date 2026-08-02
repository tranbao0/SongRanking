import os
import threading
import time

# Firing many yt-dlp requests at once is what actually trips YouTube's
# bot-check wall in practice - a burst against a large pending list came
# back 429 ("Too Many Requests") then "Sign in to confirm you're not a
# bot" on nearly every video. Unlike the Data API, yt-dlp has no quota to
# negotiate against, only YouTube's own request-rate heuristics, so the
# fix is pacing rather than budgeting: every launch waits for this long
# to have passed since the last one, no matter how many worker threads
# are free to fire immediately. Configurable because the right interval
# depends on how aggressively YouTube is currently rate-limiting, which
# isn't something this codebase can observe directly.
#
# Shared by every yt-dlp call site (metadata fetch, heatmap extraction,
# clip download) since they can all trip the same wall and YouTube
# doesn't rate-limit them separately.
_MIN_LAUNCH_INTERVAL = float(os.environ.get("YTDLP_MIN_REQUEST_INTERVAL", 0.35))
_launch_lock = threading.Lock()
_last_launch = 0.0


def throttle() -> None:
    """Block until at least _MIN_LAUNCH_INTERVAL has passed since the last
    yt-dlp launch (across every thread and call site), then claim this slot."""
    global _last_launch
    with _launch_lock:
        wait = _last_launch + _MIN_LAUNCH_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_launch = time.monotonic()


# Pacing only helps when YouTube's "confirm you're not a bot" wall is
# rate-triggered. When the IP/session itself gets flagged, no amount of
# spacing gets past it - the last resort is authenticating requests as a
# real logged-in browser session instead of an anonymous client. Only
# used as a final fallback tier (after the plain and android-client
# attempts) since it requires the named browser to be installed, closed
# or cookie-DB-readable, and logged into YouTube.
COOKIES_BROWSER = os.environ.get("YTDLP_COOKIES_BROWSER", "edge")

# Reading cookies straight out of a running Chromium browser's profile
# needs that profile's DB unlocked *and* (as of Chrome/Edge's App-Bound
# Encryption rollout) yt-dlp able to decrypt v20 cookies, which plain
# DPAPI can no longer do on its own - see
# https://github.com/yt-dlp/yt-dlp/issues/10927. A cookies.txt file
# exported once via a browser extension sidesteps both problems, so it's
# preferred over COOKIES_BROWSER whenever it's configured. Never commit
# the file this points to - it carries a live YouTube session.
COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE")


def cookie_args() -> list[str]:
    """yt-dlp CLI args for the cookies fallback tier: a cookies.txt file
    if one's configured, else falling back to reading the browser's own
    (often locked/undecryptable) cookie store directly."""
    if COOKIES_FILE:
        return ["--cookies", COOKIES_FILE]
    return ["--cookies-from-browser", COOKIES_BROWSER]
