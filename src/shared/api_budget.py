"""
Spend safeguards for the YouTube Data API quota and the Gemini request
budget. Persists a running daily count per API in data/.api_usage.json
(same keyed-by-date, auto-resetting pattern as run.py's yt-dlp
update-check cache), and raises before either budget would actually be
exceeded rather than letting a real 403/429 happen mid-run.
"""

import json
import os
import threading
from datetime import date
from pathlib import Path

USAGE_FILE = Path(__file__).parent.parent.parent / "data" / ".api_usage.json"

# batch_fetch_metadata (youtube_api.py) records usage from multiple
# worker threads concurrently - without this, two threads could both
# read the same count, increment it, and write back, silently losing one
# thread's usage from the budget.
_LOCK = threading.Lock()

# Matches the YouTube Data API's standard free-tier daily quota.
YOUTUBE_DAILY_QUOTA = int(os.environ.get("YOUTUBE_DAILY_QUOTA", 10000))

# Conservative placeholder, not a published guarantee - Gemini free-tier
# limits vary by model/tier and change over time. Set GEMINI_DAILY_LIMIT
# in .env to your actual tier's limit if you know it.
GEMINI_DAILY_LIMIT = int(os.environ.get("GEMINI_DAILY_LIMIT", 1500))

WARN_THRESHOLD = 0.8
STOP_THRESHOLD = 0.95


class QuotaExceededError(Exception):
    """Raised before a call would push usage past STOP_THRESHOLD of budget."""


def _load() -> dict:
    today = date.today().isoformat()
    data = {}
    if USAGE_FILE.exists():
        try:
            data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    if data.get("date") != today:
        data = {"date": today, "youtube_units": 0, "gemini_requests": 0}
    return data


def _save(data: dict) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(data), encoding="utf-8")


def _record(key: str, amount: int, limit: int, api_name: str) -> None:
    with _LOCK:
        data = _load()
        projected = data.get(key, 0) + amount
        ratio = projected / limit

        if ratio >= STOP_THRESHOLD:
            raise QuotaExceededError(
                f"{api_name} usage would reach {projected}/{limit} ({ratio:.0%}) - "
                f"stopping before the free-tier budget is exceeded."
            )

        data[key] = projected
        _save(data)

        if ratio >= WARN_THRESHOLD:
            print(f"[api_budget] WARNING: {api_name} usage at {projected}/{limit} ({ratio:.0%})")


def record_youtube_units(n: int) -> None:
    """Call after every YouTube Data API .execute() with that call's unit cost."""
    _record("youtube_units", n, YOUTUBE_DAILY_QUOTA, "YouTube Data API")


def record_gemini_request() -> None:
    """Call once per Gemini API request."""
    _record("gemini_requests", 1, GEMINI_DAILY_LIMIT, "Gemini API")
