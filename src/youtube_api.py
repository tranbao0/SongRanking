"""
YouTube Data API v3 drop-in replacement for metadata.py.

Swap this in place of fetch_metadata() once a YouTube API key is available.
The returned dict is identical to metadata.fetch_metadata() so pipeline.py
needs no changes.

Setup:
  1. Copy .env.example -> .env
  2. Set YOUTUBE_API_KEY=<your key> in .env
  3. pip install google-api-python-client python-dotenv
  4. Replace `from metadata import fetch_metadata` with
     `from youtube_api import fetch_metadata` in pipeline.py
"""

import os
import re
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed yet — key must be set in environment


def _get_client():
    from googleapiclient.discovery import build  # type: ignore
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "YOUTUBE_API_KEY is not set. "
            "Copy .env.example to .env and add your key."
        )
    return build("youtube", "v3", developerKey=api_key)


def _extract_video_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if not match:
        raise ValueError(f"Cannot extract video ID from URL: {url}")
    return match.group(1)


def fetch_metadata(url: str) -> dict:
    """
    Return view count, release year, and years-on-chart from a YouTube URL.
    Identical return shape to metadata.fetch_metadata().
    """
    youtube   = _get_client()
    video_id  = _extract_video_id(url)

    response = youtube.videos().list(
        part="statistics,snippet",
        id=video_id,
    ).execute()

    items = response.get("items", [])
    if not items:
        raise ValueError(f"No video found for ID: {video_id}")

    item        = items[0]
    views       = int(item["statistics"].get("viewCount", 0))
    published   = item["snippet"].get("publishedAt", "")  # ISO 8601
    release_year = int(published[:4]) if len(published) >= 4 else date.today().year
    years_on_chart = max(1, date.today().year - release_year + 1)

    return {
        "views":          views,
        "release_year":   release_year,
        "years_on_chart": years_on_chart,
    }


def search_kpop(query: str, limit: int = 50) -> list[dict]:
    """
    Search YouTube for K-pop songs and return metadata list.
    Drop-in replacement for search.search_kpop() once API key is available.
    Results sorted by view count descending.
    """
    youtube = _get_client()

    response = youtube.search().list(
        part="snippet",
        q=query,
        type="video",
        maxResults=min(limit, 50),
        videoCategoryId="10",  # Music
    ).execute()

    video_ids = [item["id"]["videoId"] for item in response.get("items", [])]
    if not video_ids:
        return []

    stats_resp = youtube.videos().list(
        part="statistics,snippet",
        id=",".join(video_ids),
    ).execute()

    songs = []
    for item in stats_resp.get("items", []):
        vid_id       = item["id"]
        views        = int(item["statistics"].get("viewCount", 0))
        published    = item["snippet"].get("publishedAt", "")
        release_year = int(published[:4]) if len(published) >= 4 else date.today().year
        years_on_chart = max(1, date.today().year - release_year + 1)

        songs.append({
            "id":             vid_id,
            "title":          item["snippet"]["title"],
            "uploader":       item["snippet"]["channelTitle"],
            "views":          views,
            "upload_date":    published[:10],
            "release_year":   release_year,
            "years_on_chart": years_on_chart,
            "url":            f"https://www.youtube.com/watch?v={vid_id}",
        })

    songs.sort(key=lambda s: s["views"], reverse=True)
    return songs
