"""
YouTube Data API v3 backend for metadata fetching and search.

Used automatically by pipeline.py when YOUTUBE_API_KEY is set in .env.
Falls back to yt-dlp (metadata.py / search.py) when the key is absent.

Setup:
  pip install google-api-python-client python-dotenv
"""

import concurrent.futures
import os
import re
from datetime import date, datetime


def _months_since(release_date: date) -> int:
    """Whole months elapsed since `release_date` (minimum 1)."""
    today = date.today()
    months = (today.year - release_date.year) * 12 + (today.month - release_date.month)
    if today.day < release_date.day:
        months -= 1
    return max(1, months)

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

    return _video_to_meta(items[0])


def _video_to_meta(item: dict) -> dict:
    views        = int(item["statistics"].get("viewCount", 0))
    published    = item["snippet"].get("publishedAt", "")
    release_date = datetime.strptime(published[:10], "%Y-%m-%d").date() if len(published) >= 10 else date.today()
    release_year = release_date.year
    return {
        "views":           views,
        "release_year":    release_year,
        "release_date":    release_date.strftime("%Y.%m.%d"),
        "years_on_chart":  max(1, date.today().year - release_year + 1),
        "months_on_chart": _months_since(release_date),
    }


def batch_fetch_metadata(urls: list[str], max_workers: int = 10) -> dict[str, dict]:
    """
    Fetch metadata for many videos at once.

    videos.list accepts up to 50 IDs per call and costs 1 quota unit
    regardless of how many IDs are packed in, so chunking into groups of
    50 cuts both request count and quota use by up to ~50x versus fetching
    one video at a time. Chunks are additionally fetched concurrently
    (each thread builds its own client, since the underlying httplib2
    transport isn't thread-safe) so a 500-song list (10 chunks) resolves
    in roughly one round-trip instead of ten sequential ones.

    Returns {url: meta_dict}. URLs with no extractable video ID or with
    no matching video (deleted/private) are omitted from the result.
    """
    url_by_id = {}
    for url in urls:
        try:
            url_by_id[_extract_video_id(url)] = url
        except ValueError:
            continue

    ids    = list(url_by_id.keys())
    chunks = [ids[i:i + 50] for i in range(0, len(ids), 50)]

    def _fetch_chunk(chunk):
        youtube  = _get_client()
        response = youtube.videos().list(
            part="statistics,snippet",
            id=",".join(chunk),
        ).execute()
        return response.get("items", [])

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for items in pool.map(_fetch_chunk, chunks):
            for item in items:
                url = url_by_id[item["id"]]
                results[url] = _video_to_meta(item)
    return results


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
        release_date = datetime.strptime(published[:10], "%Y-%m-%d").date() if len(published) >= 10 else date.today()
        release_year = release_date.year
        years_on_chart = max(1, date.today().year - release_year + 1)
        months_on_chart = _months_since(release_date)
        duration     = _parse_iso_duration(item["contentDetails"].get("duration", ""))

        songs.append({
            "id":              vid_id,
            "title":           item["snippet"]["title"],
            "uploader":        item["snippet"]["channelTitle"],
            "views":           views,
            "upload_date":     published[:10],
            "duration":        duration,
            "release_year":    release_year,
            "release_date":    release_date.strftime("%Y.%m.%d"),
            "years_on_chart":  years_on_chart,
            "months_on_chart": months_on_chart,
            "url":             f"https://www.youtube.com/watch?v={vid_id}",
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
