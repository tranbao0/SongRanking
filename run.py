"""
Command runner for the SongRanking pipeline.

Usage:
  python run.py csv
  python run.py csv --limit 10
  python run.py search
  python run.py search --q "blackpink songs"
  python run.py search --q "blackpink songs" --limit 10
  python run.py clean

  python run.py sync                      # refresh channels/videos/view snapshots for all genres
  python run.py sync --genre kpop         # refresh a single genre (repeatable)

  python run.py chart --name kpop_alltime # render a named chart from data/charts.yaml

  python run.py decouple                  # clear song groupings so they can be redone
  python run.py decouple --genre kpop     # one genre only

  python run.py regroup                   # re-derive groupings for videos decouple left ungrouped
  python run.py regroup --genre kpop      # one genre only
"""

import json
import subprocess
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from render.pipeline import run_pipeline

_YT_DLP_CHECK_CACHE = Path("data/.yt_dlp_update_check.json")
_YT_DLP_CHECK_INTERVAL = 24 * 60 * 60  # re-check at most once/day


def ensure_yt_dlp_up_to_date():
    """
    yt-dlp goes stale fast - YouTube extraction changes break older
    releases silently (e.g. PO Token requirements shifting between
    clients). Runs yt-dlp's own self-update ("yt-dlp -U"), which targets
    whichever install actually backs the "yt-dlp" command on PATH - the
    same one the pipeline's subprocess calls use - rather than assuming
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
        return  # yt-dlp missing or unresponsive - don't block the run over this

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
        choices=["csv", "search", "clean", "sync", "chart", "decouple", "regroup"],
        help="Command to run",
    )
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt (decouple mode only)")
    parser.add_argument("--q", metavar="QUERY", default="kpop songs",
                        help='Search query (search mode only, default: "kpop songs")')
    parser.add_argument("--genre", action="append", metavar="GENRE",
                        help="Genre to sync (sync mode only; repeatable, default: all known genres)")
    parser.add_argument("--name", metavar="CHART_NAME",
                        help="Chart to compute, from data/charts.yaml (chart mode only)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max songs to process")
    parser.add_argument("--no-filter", dest="no_filter", action="store_true",
                        help="Skip MV duration/title filtering (search mode only)")
    parser.add_argument("--download-workers", type=int, default=6,
                        help="Concurrent yt-dlp downloads (default: 6)")
    parser.add_argument("--encode-workers", type=int, default=3,
                        help="Concurrent ffmpeg overlay encodes (default: 3)")
    args = parser.parse_args()

    if args.command == "sync":
        from registry import discovery, catalog, snapshot

        genres = args.genre or discovery.KNOWN_GENRES
        print(f"Syncing channels for: {', '.join(genres)}")
        channel_counts = discovery.sync_channels(genres)
        for genre, count in channel_counts.items():
            print(f"  {genre}: {count} channel(s)")

        print("Syncing video catalog...")
        video_count = catalog.sync_videos(genres=genres)
        print(f"  {video_count} video(s) upserted")

        print("Taking view snapshot...")
        snapshot_count = sum(snapshot.take_snapshot(genre=g) for g in genres)
        print(f"  {snapshot_count} snapshot row(s) inserted")
        return

    if args.command == "decouple":
        from registry import decouple as decouple_mod

        genres = args.genre or [None]
        if len(genres) > 1:
            parser.error("decouple takes at most one --genre")
        decouple_mod.decouple(genre=genres[0], assume_yes=args.yes)
        return

    if args.command == "regroup":
        from registry import regroup as regroup_mod

        genres = args.genre or [None]
        if len(genres) > 1:
            parser.error("regroup takes at most one --genre")
        regroup_mod.regroup_all(genre=genres[0])
        return

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

    elif args.command == "chart":
        if not args.name:
            parser.error("chart mode requires --name (see data/charts.yaml)")
        run_pipeline(
            chart_name=args.name,
            limit=args.limit,
            download_workers=args.download_workers,
            encode_workers=args.encode_workers,
        )


if __name__ == "__main__":
    main()
