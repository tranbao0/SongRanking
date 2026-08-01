"""
YouTube Data API v3 backend for metadata fetching and search.

Used automatically by pipeline.py when YOUTUBE_API_KEY is set in .env,
falling back to yt-dlp (metadata.py / search.py) when the key is absent.
Both backends expose batch_fetch_metadata() and search_kpop() with the
same shapes, so callers never branch on which one is active.

Everything here is written around one fact: quota is the scarce resource,
not time. A videos.list call costs the same single unit whether it
carries one ID or fifty, so requests are always packed to MAX_IDS_PER_
REQUEST; a search costs a hundred times as much as a list, which is why
discovery resolves channels by other means rather than searching per
artist. Unit costs are named constants below rather than magic numbers at
the call sites, so the expensive one is impossible to reach for by
accident.

Setup:
  pip install google-api-python-client python-dotenv
"""

import concurrent.futures
import os
import re
from datetime import date, datetime

from shared import api_budget
from shared.dates import months_since
from shared.mv_filter import is_valid_mv

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Published quota costs. A search is 100x a list - the whole reason the
# registry is built from channel walks rather than per-artist searches.
UNITS_PER_LIST = 1
UNITS_PER_SEARCH = 100

# videos.list accepts this many IDs per call and bills the same one unit
# regardless, so a request is never sent narrower than it has to be.
MAX_IDS_PER_REQUEST = 50

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})")
_ISO_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def get_client():
    """
    Build an API client. Callers that fan out across threads must each
    build their own: the underlying httplib2 transport isn't thread-safe,
    and sharing one silently corrupts concurrent responses.
    """
    from googleapiclient.discovery import build  # type: ignore

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("YOUTUBE_API_KEY is not set. Add it to .env.")
    return build("youtube", "v3", developerKey=api_key)


def extract_video_id(url: str) -> str:
    """The 11-character video ID in a watch or youtu.be URL."""
    match = _VIDEO_ID_RE.search(url)
    if not match:
        raise ValueError(f"Cannot extract video ID from URL: {url}")
    return match.group(1)


def parse_iso_duration(duration_str: str) -> int:
    """ISO 8601 duration (PT1H2M3S) as whole seconds. Unparseable is 0."""
    match = _ISO_DURATION_RE.match(duration_str or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def chunked_ids(video_ids: list[str]) -> list[list[str]]:
    """Split IDs into the widest requests the API will accept."""
    return [
        video_ids[i:i + MAX_IDS_PER_REQUEST]
        for i in range(0, len(video_ids), MAX_IDS_PER_REQUEST)
    ]


def _published_date(item: dict) -> date:
    """
    An item's publish date, falling back to today when the field is
    missing or truncated - the alternative is raising on an otherwise
    usable video, and every consumer of this treats the date as
    presentational.
    """
    published = item.get("snippet", {}).get("publishedAt", "")
    if len(published) < 10:
        return date.today()
    return datetime.strptime(published[:10], "%Y-%m-%d").date()


def _meta_from_item(item: dict) -> dict:
    """The metadata shape shared with the yt-dlp backend."""
    release_date = _published_date(item)
    return {
        "views":           int(item["statistics"].get("viewCount", 0)),
        "release_year":    release_date.year,
        "release_date":    release_date.strftime("%Y.%m.%d"),
        "years_on_chart":  max(1, date.today().year - release_date.year + 1),
        "months_on_chart": months_since(release_date),
    }


def _search_result_from_item(item: dict) -> dict:
    """One search hit, in the shape search.py's yt-dlp backend returns."""
    video_id = item["id"]
    return {
        **_meta_from_item(item),
        "id":          video_id,
        "title":       item["snippet"]["title"],
        "uploader":    item["snippet"]["channelTitle"],
        "upload_date": item["snippet"].get("publishedAt", "")[:10],
        "duration":    parse_iso_duration(item["contentDetails"].get("duration", "")),
        "url":         f"https://www.youtube.com/watch?v={video_id}",
    }


def batch_fetch_metadata(urls: list[str], max_workers: int = 10) -> dict[str, dict]:
    """
    Fetch metadata for many videos at once, returning {url: meta}.

    Packing IDs 50 to a request cuts quota use by up to 50x against
    fetching one at a time, and the packed requests are then run
    concurrently, so a 500-song list resolves in roughly one round trip
    rather than ten sequential ones.

    URLs with no extractable video ID, and videos that no longer exist,
    are simply absent from the result. Callers already treat a missing
    URL as "couldn't fetch, skip" (see chart_state.pre_fetch_all), so
    partial results degrade the same way a single dead video does.
    """
    url_by_id: dict[str, str] = {}
    for url in urls:
        try:
            url_by_id[extract_video_id(url)] = url
        except ValueError:
            continue

    def _fetch(chunk: list[str]) -> list[dict]:
        # Budget is checked per chunk rather than up front, and its
        # refusal is caught here rather than raised: chunks run
        # concurrently, so one hitting the limit must not discard results
        # its siblings already fetched.
        try:
            api_budget.record_youtube_units(UNITS_PER_LIST)
        except api_budget.QuotaExceededError as e:
            print(f"  [youtube_api] {e}")
            return []
        response = get_client().videos().list(
            part="statistics,snippet", id=",".join(chunk),
        ).execute()
        return response.get("items", [])

    chunks = chunked_ids(list(url_by_id))
    if not chunks:
        return {}

    results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for items in pool.map(_fetch, chunks):
            for item in items:
                results[url_by_id[item["id"]]] = _meta_from_item(item)
    return results


def search_kpop(query: str, limit: int = 50, filter_mv: bool = True) -> list[dict]:
    """
    Search YouTube and return results sorted by view count, descending.
    Same shape as search.search_kpop(), so pipeline.py can use either.

    Two calls: search.list finds IDs (100 units), then one videos.list
    fetches the stats and durations for all of them (1 unit). Over-fetches
    when filtering, since MV filtering happens after the search and would
    otherwise return short. The API caps a search at 50 results.
    """
    youtube = get_client()
    fetch_n = min(MAX_IDS_PER_REQUEST, limit * 2 if filter_mv else limit)

    api_budget.record_youtube_units(UNITS_PER_SEARCH)
    search_response = youtube.search().list(
        part="snippet", q=query, type="video", maxResults=fetch_n,
        videoCategoryId="10",  # Music
    ).execute()

    video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
    if not video_ids:
        return []

    api_budget.record_youtube_units(UNITS_PER_LIST)
    details = youtube.videos().list(
        part="statistics,snippet,contentDetails", id=",".join(video_ids),
    ).execute()

    songs = [_search_result_from_item(item) for item in details.get("items", [])]

    if filter_mv:
        kept = [s for s in songs if is_valid_mv(s["title"], s["duration"])]
        if len(kept) != len(songs):
            print(f"  Filtered out {len(songs) - len(kept)} non-MV result(s) via YouTube API.")
        songs = kept

    songs.sort(key=lambda s: s["views"], reverse=True)
    return songs[:limit]
