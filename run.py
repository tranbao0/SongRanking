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

  python run.py audit                     # force a grouping-quality audit on the local working DB (no pull/push)

  python run.py pull                      # merge Turso's current state into the local working copy
  python run.py pull --override           # overwrite the local working copy with Turso's current state
  python run.py push                      # merge the local working copy's current state into Turso
  python run.py push --override           # overwrite Turso with the local working copy's current state

To stop a run, press Ctrl+C once and let it wrap up the current step
(finishes any in-flight Turso push/pull so the hosted registry is never
left half-written). Press Ctrl+C again to force an immediate exit -
this can leave orphaned yt-dlp/ffmpeg subprocesses behind, so prefer a
single press when you can. Don't kill python.exe from Task Manager or
`taskkill` while a `sync`/`push`/`pull` is running - that skips this
entirely.
"""

import json
import subprocess
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from render.pipeline import run_pipeline
from shared import shutdown

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
        choices=["csv", "search", "clean", "sync", "chart", "decouple", "regroup", "audit", "pull", "push"],
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
    parser.add_argument("--override", action="store_true",
                        help="Replace the destination outright instead of merging "
                             "(pull/push mode only; merge is the default)")
    args = parser.parse_args()

    if args.command == "sync":
        from registry import db, discovery, catalog, snapshot, grouping_audit
        from shared import gemini_client

        genres = args.genre or discovery.KNOWN_GENRES
        print(f"Syncing channels for: {', '.join(genres)}")
        # A long-running sync only ever reads GEMINI_API_KEY once, at
        # process start - editing .env mid-run has no effect until the
        # process is restarted. Printed once here, up front, so that's
        # never something to infer from behavior later in the run.
        if gemini_client.is_available():
            print("  AI grouping (Gemini): enabled")

        # Steps 1 and 3 are the only network calls to the hosted registry
        # in this whole command - see db.py's module docstring. Everything
        # between them (discovery, catalog, snapshot, the audit agent)
        # reads/writes the local working copy pull_to_local() just seeded,
        # at local-disk speed.
        print("Fetching current registry from Turso...")
        db.pull_to_local()

        audit_snapshot = grouping_audit.snapshot_before_sync()

        channel_counts = discovery.sync_channels(genres)
        for genre, count in channel_counts.items():
            print(f"  {genre}: {count} channel(s)")

        print("Syncing video catalog...")
        video_count = catalog.sync_videos(genres=genres)
        print(f"  {video_count} video(s) upserted")

        print("Taking view snapshot...")
        snapshot_count = sum(snapshot.take_snapshot(genre=g) for g in genres)
        print(f"  {snapshot_count} snapshot row(s) inserted")

        grouping_audit.audit_after_sync(audit_snapshot)

        print("Pushing updated registry to Turso...")
        db.push_from_local()
        return

    if args.command == "decouple":
        from registry import db, decouple as decouple_mod

        genres = args.genre or [None]
        if len(genres) > 1:
            parser.error("decouple takes at most one --genre")
        print("Fetching current registry from Turso...")
        db.pull_to_local()
        result = decouple_mod.decouple(genre=genres[0], assume_yes=args.yes)
        if result > 0:
            print("Pushing updated registry to Turso...")
            db.push_from_local()
        return

    if args.command == "regroup":
        from registry import db, regroup as regroup_mod

        genres = args.genre or [None]
        if len(genres) > 1:
            parser.error("regroup takes at most one --genre")
        print("Fetching current registry from Turso...")
        db.pull_to_local()
        regroup_mod.regroup_all(genre=genres[0])
        print("Pushing updated registry to Turso...")
        db.push_from_local()
        return

    if args.command == "audit":
        from registry import grouping_audit

        grouping_audit.run_manual_audit()
        return

    if args.command == "pull":
        from registry import db

        mode = "overwriting" if args.override else "merging into"
        print(f"Fetching current registry from Turso ({mode} the local working copy)...")
        db.pull_to_local(override=args.override)
        print(f"  Local working copy updated at {db.LOCAL_WORKING_DB_PATH}")
        return

    if args.command == "push":
        from registry import db

        mode = "overwriting" if args.override else "merging into"
        print(f"Pushing local working copy to Turso ({mode} the hosted registry)...")
        db.push_from_local(override=args.override)
        print("  Done.")
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
    shutdown.install()
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(130)
