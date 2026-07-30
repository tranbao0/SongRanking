"""
Command runner — cross-platform replacement for the Makefile.

Usage:
  python run.py csv
  python run.py csv --limit 10
  python run.py search
  python run.py search --q "blackpink songs"
  python run.py search --q "blackpink songs" --limit 10
  python run.py clean
"""

import json
import subprocess
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from pipeline import run_pipeline

_YT_DLP_CHECK_CACHE = Path("data/.yt_dlp_update_check.json")
_YT_DLP_CHECK_INTERVAL = 24 * 60 * 60  # re-check at most once/day


def ensure_yt_dlp_up_to_date():
    """
    yt-dlp goes stale fast — YouTube extraction changes break older
    releases silently (e.g. PO Token requirements shifting between
    clients). Runs yt-dlp's own self-update ("yt-dlp -U"), which targets
    whichever install actually backs the "yt-dlp" command on PATH — the
    same one the pipeline's subprocess calls use — rather than assuming
    it's pip-managed. Checked at most once/day; skipped silently if
    yt-dlp isn't found or the update check times out.
    """
    try:
        if _YT_DLP_CHECK_CACHE.exists():
            checked_at = json.loads(_YT_DLP_CHECK_CACHE.read_text()).get("checked_at", 0)
            if time.time() - checked_at < _YT_DLP_CHECK_INTERVAL:
                return
    except (json.JSONDecodeError, OSError):
        pass

    try:
        result = subprocess.run(["yt-dlp", "-U"], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # yt-dlp missing or unresponsive — don't block the run over this

    _YT_DLP_CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _YT_DLP_CHECK_CACHE.write_text(json.dumps({"checked_at": time.time()}))

    if result.returncode == 0 and result.stdout.strip():
        print(f"[yt-dlp] {result.stdout.strip().splitlines()[-1]}")


def main():
    parser = argparse.ArgumentParser(
        description="SongRanking command runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        choices=["csv", "search", "clean"],
        help="Command to run",
    )
    parser.add_argument("--q", metavar="QUERY", default="kpop songs",
                        help='Search query (search mode only, default: "kpop songs")')
    parser.add_argument("--limit", type=int, default=None,
                        help="Max songs to process")
    parser.add_argument("--no-filter", dest="no_filter", action="store_true",
                        help="Skip MV duration/title filtering (search mode only)")
    parser.add_argument("--download-workers", type=int, default=6,
                        help="Concurrent yt-dlp downloads (default: 6)")
    parser.add_argument("--encode-workers", type=int, default=3,
                        help="Concurrent ffmpeg overlay encodes (default: 3)")
    args = parser.parse_args()

    ensure_yt_dlp_up_to_date()

    if args.command == "csv":
        run_pipeline(
            limit=args.limit,
            download_workers=args.download_workers,
            encode_workers=args.encode_workers,
        )

    elif args.command == "search":
        run_pipeline(
            search=args.q,
            limit=args.limit,
            no_filter=args.no_filter,
            download_workers=args.download_workers,
            encode_workers=args.encode_workers,
        )

    elif args.command == "clean":
        run_pipeline(clean_titles=True)


if __name__ == "__main__":
    main()
