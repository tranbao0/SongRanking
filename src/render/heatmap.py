"""
YouTube "most replayed" heatmap detection, used to pick where a song's
15-second clip starts instead of a fixed 00:01:00.

YouTube's own web player shows a heat graph under the scrubber built from
per-segment replay intensity ("heat markers") baked into the same watch-page
player response yt-dlp already parses for every other metadata field. yt-dlp
exposes it natively as info_dict["heatmap"] -- no separate/deprecated API
involved, and nothing to fall back to there.

The YouTube Data API v3 has no equivalent: it exposes no heatmap, retention,
or "most replayed" data at all (that's YouTube Analytics API territory, and
even that doesn't expose this specific signal for arbitrary third-party
videos). So there's no alternate official backend to wire up here regardless
of whether YOUTUBE_API_KEY is set -- this module always goes through yt-dlp.

yt-dlp's heatmap field is also always the raw per-segment marker list
(start_time/end_time/value, value normalized 0-1), never a pre-labeled list
of "most replayed" ranges -- YouTube doesn't publish it that way, so there's
no richer variant to prefer. Sections are derived here by grouping
consecutive markers whose value clears HEAT_THRESHOLD.
"""

import random

import yt_dlp

from shared.ytdlp_throttle import throttle, COOKIES_BROWSER, COOKIES_FILE

CLIP_DURATION  = 15.0
HEAT_THRESHOLD = 0.6
DEFAULT_START  = 60.0  # matches the old fixed 00:01:00


def _extract_info(url, player_client=None, use_cookies=False):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    if use_cookies:
        # A cookies.txt file (see ytdlp_throttle.COOKIES_FILE) is preferred
        # over reading the browser's own cookie store directly - same
        # reasoning as encoding.download_song's matching fallback tier.
        if COOKIES_FILE:
            opts["cookiefile"] = COOKIES_FILE
        else:
            opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    throttle()
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _fetch_heatmap(url):
    """
    Returns (heatmap, duration). heatmap is None when YouTube hasn't
    published one for this video (too new/short/private, or simply never
    got enough watch-time data for one to be generated).
    """
    try:
        info = _extract_info(url)
    except yt_dlp.utils.DownloadError:
        # Mirrors encoding.download_song's client fallback: some
        # videos/sessions get bucketed into a restricted experiment that
        # the default client can't extract at all.
        try:
            info = _extract_info(url, player_client="android")
        except yt_dlp.utils.DownloadError:
            # Last resort when the wall is an IP/session flag rather than
            # a client-experiment bucket - see encoding.download_song's
            # matching third tier.
            info = _extract_info(url, use_cookies=True)
    return info.get("heatmap"), info.get("duration")


def _hot_sections(heatmap):
    """Group consecutive heat markers with value >= HEAT_THRESHOLD into runs."""
    sections = []
    current = None
    for point in sorted(heatmap, key=lambda p: p["start_time"]):
        if point["value"] >= HEAT_THRESHOLD:
            if current is None:
                current = {"start_time": point["start_time"], "end_time": point["end_time"]}
            else:
                current["end_time"] = point["end_time"]
        elif current is not None:
            sections.append(current)
            current = None
    if current is not None:
        sections.append(current)
    return sections


def pick_clip(url, clip_duration=CLIP_DURATION, default_start=DEFAULT_START):
    """
    Pick a `clip_duration`-second (start, end) window in seconds to download
    for `url`.

    When a heatmap is available: collapses it into hot sections (>=
    HEAT_THRESHOLD intensity), rolls a dice among them if there's more than
    one, then starts the clip 1 second before that section begins rather
    than exactly on top of it. Falls back to the video's single hottest
    marker if no section clears the threshold, and to `default_start` if
    the video has no heatmap at all or extraction fails outright.
    """
    try:
        heatmap, duration = _fetch_heatmap(url)
    except Exception:
        heatmap, duration = None, None

    if not heatmap:
        return default_start, default_start + clip_duration

    sections = _hot_sections(heatmap)
    section = random.choice(sections) if sections else max(heatmap, key=lambda p: p["value"])

    start = max(section["start_time"] - 1.0, 0.0)
    end   = start + clip_duration
    if duration and end > duration:
        end   = duration
        start = max(end - clip_duration, 0.0)
    return start, end
