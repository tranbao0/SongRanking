"""
YouTube Data API v3 backend for metadata fetching and search.

Used automatically by pipeline.py when YOUTUBE_API_KEY is set in .env.
Falls back to yt-dlp (metadata.py / search.py) when the key is absent.

Setup:
  pip install google-api-python-client python-dotenv
"""

import os
import re
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Mirror the same blocklist used by search.py so filtering is consistent
# whether we use yt-dlp search or the YouTube API.
_BLOCKLIST = re.compile(
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
    r"|mix"
    r")\b",
    re.IGNORECASE,
)

_MIN_DURATION = 90
_MAX_DURATION = 720


def _get_client():
    from googleapiclient.discovery import build  # type: ignore
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError(
            "YOUTUBE_API_KEY is not set. Add it to .env."
        )
    return build("youtube", "v3", developerKey=api_key)


def _extract_video_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if not match:
        raise ValueError(f"Cannot extract video ID from URL: {url}")
    return match.group(1)


def _parse_iso_duration(duration_str: str) -> int:
    """Convert ISO 8601 duration string (PT1H2M3S) to total seconds."""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str or "")
    if not match:
        return 0
    hours   = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def fetch_metadata(url: str) -> dict:
    """
    Return view count, release year, and years-on-chart for a YouTube URL.
    Drop-in replacement for metadata.fetch_metadata().
    """
    youtube  = _get_client()
    video_id = _extract_video_id(url)

    response = youtube.videos().list(
        part="statistics,snippet",
        id=video_id,
    ).execute()

    items = response.get("items", [])
    if not items:
        raise ValueError(f"No video found for ID: {video_id}")

    item         = items[0]
    views        = int(item["statistics"].get("viewCount", 0))
    published    = item["snippet"].get("publishedAt", "")
    release_year = int(published[:4]) if len(published) >= 4 else date.today().year
    years_on_chart = max(1, date.today().year - release_year + 1)

    return {
        "views":          views,
        "release_year":   release_year,
        "years_on_chart": years_on_chart,
    }


def search_kpop(query: str, limit: int = 50, filter_mv: bool = True) -> list[dict]:
    """
    Search YouTube for K-pop songs and return metadata list.
    Drop-in replacement for search.search_kpop().

    Uses a two-step API call:
      1. search.list  — finds video IDs matching the query
      2. videos.list  — fetches stats + duration for those IDs

    Results are sorted by view count descending.
    YouTube API caps search results at 50 per request.
    """
    youtube  = _get_client()
    fetch_n  = min(50, limit * 2 if filter_mv else limit)

    search_resp = youtube.search().list(
        part="snippet",
        q=query,
        type="video",
        maxResults=fetch_n,
        videoCategoryId="10",  # Music
    ).execute()

    video_ids = [item["id"]["videoId"] for item in search_resp.get("items", [])]
    if not video_ids:
        return []

    stats_resp = youtube.videos().list(
        part="statistics,snippet,contentDetails",
        id=",".join(video_ids),
    ).execute()

    songs = []
    for item in stats_resp.get("items", []):
        vid_id       = item["id"]
        views        = int(item["statistics"].get("viewCount", 0))
        published    = item["snippet"].get("publishedAt", "")
        release_year = int(published[:4]) if len(published) >= 4 else date.today().year
        years_on_chart = max(1, date.today().year - release_year + 1)
        duration     = _parse_iso_duration(item["contentDetails"].get("duration", ""))

        songs.append({
            "id":             vid_id,
            "title":          item["snippet"]["title"],
            "uploader":       item["snippet"]["channelTitle"],
            "views":          views,
            "upload_date":    published[:10],
            "duration":       duration,
            "release_year":   release_year,
            "years_on_chart": years_on_chart,
            "url":            f"https://www.youtube.com/watch?v={vid_id}",
        })

    if filter_mv:
        before  = len(songs)
        songs   = [
            s for s in songs
            if _MIN_DURATION <= s["duration"] <= _MAX_DURATION
            and not _BLOCKLIST.search(s["title"])
        ]
        removed = before - len(songs)
        if removed:
            print(f"  Filtered out {removed} non-MV result(s) via YouTube API.")

    songs.sort(key=lambda s: s["views"], reverse=True)
    return songs[:limit]
