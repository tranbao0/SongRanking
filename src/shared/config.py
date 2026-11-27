import os
import sys
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
CHARTS_DIR = DATA_DIR / "charts"
CHANNELS_DIR = DATA_DIR / "channels"
ASSETS_DIR = REPO_ROOT / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"
FONTS_DIR = ASSETS_DIR / "fonts"
CLIPS_DIR = ASSETS_DIR / "clips"
RAW_CACHE_DIR = CLIPS_DIR / "raw"
STYLE_FILE = TEMPLATES_DIR / "style.json"
CHARTS_FILE = DATA_DIR / "charts.yaml"
SONGS_CSV = DATA_DIR / "songs.csv"
LOCAL_WORKING_DB_PATH = DATA_DIR / ".registry_working.db"
DATA_REPO_PATH = DATA_DIR / ".registry-data"
USAGE_FILE = DATA_DIR / ".api_usage.json"
YT_DLP_CHECK_CACHE = DATA_DIR / ".yt_dlp_update_check.json"
AUDIT_DIR = DATA_DIR / ".grouping-audit"
BUNDLED_FONT_BOLD = FONTS_DIR / "NanumGothic-Bold.ttf"
BUNDLED_FONT_REGULAR = FONTS_DIR / "NanumGothic-Regular.ttf"
DEFAULT_POT_BASE_URL = "http://127.0.0.1:4416"


@cache
def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env")


def configure_console() -> None:
    # Windows console and pipe encodings can omit Hangul.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def youtube_api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "").strip()


def gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip()


def registry_data_repo_url() -> str | None:
    return os.environ.get("REGISTRY_DATA_REPO_URL")


# The YouTube Data API standard free-tier daily quota.
def youtube_daily_quota() -> int:
    return int(os.environ.get("YOUTUBE_DAILY_QUOTA", 10000))


# A request cap bounds spending when Gemini bills by token.
def gemini_daily_limit() -> int:
    return int(os.environ.get("GEMINI_DAILY_LIMIT", 100))


def ytdlp_min_request_interval() -> float:
    return float(os.environ.get("YTDLP_MIN_REQUEST_INTERVAL", 1))


# Bound each attempt so a stalled client can fall through to the next one.
def ytdlp_download_attempt_timeout() -> float:
    return float(os.environ.get("YTDLP_DOWNLOAD_ATTEMPT_TIMEOUT", 60))


def ytdlp_cookies_browser() -> str:
    return os.environ.get("YTDLP_COOKIES_BROWSER", "edge")


def ytdlp_cookies_file() -> str | None:
    return os.environ.get("YTDLP_COOKIES_FILE")


def ytdlp_pot_base_url() -> str:
    return os.environ.get("YTDLP_POT_BASE_URL", DEFAULT_POT_BASE_URL)


def ytdlp_min_raw_height() -> int:
    return int(os.environ.get("YTDLP_MIN_RAW_HEIGHT", 1440))


def grouping_agent_cli() -> list[str]:
    return os.environ.get(
        "GROUPING_AGENT_CLI",
        "claude -p --verbose --output-format stream-json --permission-mode bypassPermissions",
    ).split()


def grouping_audit_every_n_syncs() -> int:
    return int(os.environ.get("GROUPING_AUDIT_EVERY_N_SYNCS", 7))


def grouping_audit_volume_threshold() -> int:
    return int(os.environ.get("GROUPING_AUDIT_VOLUME_THRESHOLD", 50))


def grouping_agent_timeout() -> int:
    return int(os.environ.get("GROUPING_AGENT_TIMEOUT", 3600 * 5))
