import os
import sys
import threading
import time
import urllib.error
import urllib.request

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
_MIN_LAUNCH_INTERVAL = float(os.environ.get("YTDLP_MIN_REQUEST_INTERVAL", 1))
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


# Always invoke yt-dlp as `python -m yt_dlp` rather than a bare `yt-dlp` on
# PATH. A Chocolatey/standalone binary and a pip-installed yt-dlp can both
# exist on the same machine, and only the pip one sees pip-installed
# plugins (notably bgutil-ytdlp-pot-provider). Using the active interpreter
# keeps CLI downloads and the heatmap Python API on the same install.
def ytdlp_cmd(*args: str) -> list[str]:
    """Argv for a yt-dlp subprocess in this Python env (so pip plugins load).

    Always enables the GitHub-hosted EJS challenge-solver scripts: web-family
    clients (mweb, tv_simply, web_embedded, …) need them to resolve n/sig
    after a PO Token unlocks the format URLs. Without this flag those clients
    can retrieve a token and still end up with "Only images are available".
    """
    return [
        sys.executable, "-m", "yt_dlp",
        "--remote-components", "ejs:github",
        *args,
    ]



# Fallbacks tried (in order) when the default client rotation fails, before
# cookies. Default already covers android_vr + web_safari (full ladder /
# HLS up to 1080p, no auth). Kept short on purpose: each failed client is
# a full yt-dlp process (often 30-90s), and a long ladder turns one bad
# video into a multi-minute stall under the download pool.
#
#   mweb          - full-res adaptive with a Web PO Token (bgutil)
#   web_embedded  - no PO Token required; embeddable videos only
#   android       - usually works without auth, but only ~360p (format 18);
#                   last player-client safety net before cookies
#
# Intentionally omitted:
#   ios        - needs an iOSGuard PO Token; bgutil only issues Web tokens,
#                so it almost always fails and just burns wall-clock
#   tv_simply  - Web-PO capable, but in practice fails alongside mweb when
#                the session is already hostile, adding another slow miss
#   tv_embedded - removed from yt-dlp; pinning it is a no-op
DOWNLOAD_FALLBACK_CLIENTS = ["mweb", "web_embedded", "android"]

# Soft ceiling on a single yt-dlp download attempt. A ~20s section at 1080p+
# almost never needs more than this; without it, a hung client negotiation
# can burn 2-3 minutes before the fallback ladder moves on. Override via
# YTDLP_DOWNLOAD_ATTEMPT_TIMEOUT (seconds).
DOWNLOAD_ATTEMPT_TIMEOUT = float(os.environ.get("YTDLP_DOWNLOAD_ATTEMPT_TIMEOUT", 60))


def download_fallback_clients() -> list[str]:
    """Player-client fallbacks for this moment, with dead tiers dropped.

    `mweb` needs the bgutil POT server for full-res adaptive formats. When
    that server isn't reachable, trying mweb is a slow guaranteed miss -
    skip it and go straight to web_embedded / android.
    """
    clients = list(DOWNLOAD_FALLBACK_CLIENTS)
    if "mweb" in clients and not pot_provider_reachable():
        return [c for c in clients if c != "mweb"]
    return clients


def quality_upgrade_clients() -> list[str]:
    """Fallbacks for re-fetching a short/low-res cache entry at higher height.

    Same as download_fallback_clients, minus `android` - that client only
    exposes progressive format 18 (~360p), so it can never raise a clip
    that already failed the min-height check.
    """
    return [c for c in download_fallback_clients() if c != "android"]


# Pacing only helps when YouTube's "confirm you're not a bot" wall is
# rate-triggered. When the IP/session itself gets flagged, no amount of
# spacing gets past it - the last resort is authenticating requests as a
# real logged-in browser session instead of an anonymous client. Only
# used as a final fallback tier (after the plain and player-client
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

# bgutil-ytdlp-pot-provider's HTTP server. The pip plugin defaults to this
# URL; override only if you published the server on a different host/port.
POT_PROVIDER_BASE_URL = os.environ.get("YTDLP_POT_BASE_URL", "http://127.0.0.1:4416")

# Avoid pinging the POT server on every fallback decision during a chart
# run - reachability rarely flips mid-pipeline.
_POT_REACHABLE_TTL = 60.0
_pot_reachable_lock = threading.Lock()
_pot_reachable_cache: bool | None = None
_pot_reachable_checked_at = 0.0


def cookie_args() -> list[str]:
    """yt-dlp CLI args for the cookies fallback tier: a cookies.txt file
    if one's configured, else falling back to reading the browser's own
    (often locked/undecryptable) cookie store directly."""
    if COOKIES_FILE:
        return ["--cookies", COOKIES_FILE]
    return ["--cookies-from-browser", COOKIES_BROWSER]


def pot_extractor_args() -> list[str]:
    """Extra --extractor-args when the POT server isn't on the default URL.

    Empty when using the plugin default (127.0.0.1:4416) so we don't pass
    a redundant arg on every call. Non-default base_url is required for
    the HTTP provider to find the server.
    """
    if POT_PROVIDER_BASE_URL.rstrip("/") == "http://127.0.0.1:4416":
        return []
    return ["--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_BASE_URL}"]


def pot_provider_reachable(timeout: float = 1.0) -> bool:
    """Whether bgutil's HTTP POT server answers /ping right now.

    Result is memoized briefly so a chart run's fallback walk doesn't pay
    a fresh HTTP round-trip on every song.
    """
    global _pot_reachable_cache, _pot_reachable_checked_at
    now = time.monotonic()
    with _pot_reachable_lock:
        if (
            _pot_reachable_cache is not None
            and now - _pot_reachable_checked_at < _POT_REACHABLE_TTL
        ):
            return _pot_reachable_cache
    try:
        with urllib.request.urlopen(
            f"{POT_PROVIDER_BASE_URL.rstrip('/')}/ping", timeout=timeout,
        ) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        ok = False
    with _pot_reachable_lock:
        _pot_reachable_cache = ok
        _pot_reachable_checked_at = time.monotonic()
    return ok
