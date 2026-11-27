"""
Spend safeguards for the YouTube Data API quota and the Gemini request
budget. Persists a running daily count per API in data/.api_usage.json
(same keyed-by-date, auto-resetting pattern as run.py's yt-dlp
update-check cache), and raises before either budget would actually be
exceeded rather than letting a real 403/429 happen mid-run.
"""

import json
import sys
import threading
from datetime import date
from pathlib import Path

from shared import config

USAGE_FILE = config.USAGE_FILE

# batch_fetch_metadata (youtube_api.py) records usage from multiple
# worker threads concurrently - without this, two threads could both
# read the same count, increment it, and write back, silently losing one
# thread's usage from the budget.
_LOCK = threading.Lock()

WARN_THRESHOLD = 0.8
STOP_THRESHOLD = 0.95


class QuotaExceededError(Exception):
    """Raised before a call would push usage past STOP_THRESHOLD of budget."""


_SETTINGS = {
    "YOUTUBE_DAILY_QUOTA": config.youtube_daily_quota,
    "GEMINI_DAILY_LIMIT": config.gemini_daily_limit,
}


def __getattr__(name):
    if name in _SETTINGS:
        return _SETTINGS[name]()
    raise AttributeError(name)


_module = sys.modules[__name__]


def _load(usage_file: Path) -> dict:
    today = date.today().isoformat()
    data = {}
    if usage_file.exists():
        try:
            data = json.loads(usage_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    if data.get("date") != today:
        data = {"date": today, "youtube_units": 0, "gemini_requests": 0}
    return data


def _save(data: dict, usage_file: Path) -> None:
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps(data), encoding="utf-8")


def _record(key: str, amount: int, limit: int, api_name: str, usage_file: Path | None) -> None:
    path = USAGE_FILE if usage_file is None else usage_file
    with _LOCK:
        data = _load(path)
        previous = data.get(key, 0)
        projected = previous + amount
        ratio = projected / limit

        if ratio >= STOP_THRESHOLD:
            # Deliberately not printed here: callers hit this on every
            # remaining chunk/request once the budget is gone (hundreds of
            # times in one run), so raising and letting each caller report
            # it once - rather than here, on every attempt - is what keeps
            # this from flooding the log. See youtube_api.batch_fetch_metadata
            # and gemini_client.call_gemini for the once-per-run prints.
            raise QuotaExceededError(
                f"{api_name} usage would reach {projected}/{limit} ({ratio:.0%}) - "
                f"stopping before the free-tier budget is exceeded."
            )

        data[key] = projected
        _save(data, path)

        # Edge-triggered (only the call that crosses the threshold prints),
        # not level-triggered - otherwise every call for the rest of the day
        # once past 80% repeats the same warning.
        if ratio >= WARN_THRESHOLD > previous / limit:
            print(f"[api_budget] WARNING: {api_name} usage at {projected}/{limit} ({ratio:.0%})")


def record_youtube_units(n: int, *, usage_file: Path | None = None) -> None:
    """Call after every YouTube Data API .execute() with that call's unit cost."""
    _record("youtube_units", n, _module.YOUTUBE_DAILY_QUOTA, "YouTube Data API", usage_file)


def record_gemini_request(*, usage_file: Path | None = None) -> None:
    """Call once per Gemini API request."""
    _record("gemini_requests", 1, _module.GEMINI_DAILY_LIMIT, "Gemini API", usage_file)
