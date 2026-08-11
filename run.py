"""
Command runner for the SongRanking pipeline.

Usage:
  python run.py csv
  python run.py csv --limit 10
  python run.py csv --file data/charts/kpop_alltime.csv --offline  # rebuild a chart's video with no network/API calls
  python run.py search
  python run.py search --q "blackpink songs"
  python run.py search --q "blackpink songs" --limit 10
  python run.py clean

  python run.py sync                      # refresh channels/videos/view snapshots for all genres
  python run.py sync --genre kpop         # refresh a single genre (repeatable)

  python run.py chart --name kpop_alltime # render a named chart from data/charts.yaml

  python run.py decouple                  # clear song groupings so they can be redone (local working DB only, no pull/push)
  python run.py decouple --genre kpop     # one genre only

python run.py regroup                   # free tiers 1/2, then CLI agent for leftovers (no Gemini)
python run.py regroup --genre kpop      # one genre only

  python run.py merge-versions            # merge already-grouped remix/acoustic/language/etc. version pairs (local working DB only, no pull/push)
  python run.py merge-versions --genre kpop  # one genre only

  python run.py audit                     # force a grouping-quality audit on the local working DB (no pull/push)
  python run.py audit --prompt "..."      # same, with extra one-off instructions appended to the audit prompt

  python run.py pull                      # rebuild the local working copy from the data repo's current state
  python run.py push                      # publish the local working copy's current state to the data repo

To stop a run, press Ctrl+C once and let it wrap up the current step
(finishes any in-flight git commit/push so the data repo is never left
half-written). Press Ctrl+C again to force an immediate exit - that also
force-kills helper subprocesses (ffmpeg / yt-dlp children) so they aren't
left orphaned. Don't kill python.exe from Task Manager or `taskkill`
while a `sync`/`push`/`pull` is running - that skips this entirely.
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
    clients). Updates via `python -m yt_dlp -U` so the pip install that
    the pipeline actually invokes (and that loads pip plugins like
    bgutil-ytdlp-pot-provider) stays current - not a separate Chocolatey/
    standalone binary that may also sit on PATH. Checked at most once/day;
    skipped silently if the update check times out.
    """
    try:
        if _YT_DLP_CHECK_CACHE.exists():
            checked_at = json.loads(_YT_DLP_CHECK_CACHE.read_text()).get("checked_at", 0)
            if time.time() - checked_at < _YT_DLP_CHECK_INTERVAL:
                return
    except (json.JSONDecodeError, OSError):
        pass

    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "-U"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # yt-dlp missing or unresponsive - don't block the run over this

    _YT_DLP_CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _YT_DLP_CHECK_CACHE.write_text(json.dumps({"checked_at": time.time()}))

    if result.returncode == 0 and result.stdout.strip():
        print(f"[yt-dlp] {result.stdout.strip().splitlines()[-1]}")


def warn_if_pot_provider_down():
    """
    mweb fallbacks need bgutil's HTTP POT server for full-res
    adaptive formats. Missing it isn't fatal (android still catches at
    360p, then cookies), but it's worth a one-line heads-up before a
    chart/csv/search run walks that chain.
    """
    from shared.ytdlp_throttle import pot_provider_reachable, POT_PROVIDER_BASE_URL
    if pot_provider_reachable():
        print(f"[yt-dlp] PO Token provider reachable at {POT_PROVIDER_BASE_URL}")
        return
    print(
        f"[yt-dlp] PO Token provider not reachable at {POT_PROVIDER_BASE_URL} - "
        "mweb high-res fallbacks will degrade. Start it with: "
        f"node %USERPROFILE%\\bgutil-ytdlp-pot-provider\\server\\build\\main.js"
    )


def main():
    parser = argparse.ArgumentParser(
        description="SongRanking command runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        choices=["csv", "search", "clean", "sync", "chart", "decouple", "regroup", "merge-versions",
                 "audit", "pull", "push"],
        help="Command to run",
    )
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt (decouple/merge-versions modes only)")
    parser.add_argument("--q", metavar="QUERY", default="kpop songs",
                        help='Search query (search mode only, default: "kpop songs")')
    parser.add_argument("--genre", action="append", metavar="GENRE",
                        help="Genre to scope to (sync/decouple/regroup/merge-versions modes; "
                             "repeatable for sync, default: all known genres)")
    parser.add_argument("--name", metavar="CHART_NAME",
                        help="Chart to compute, from data/charts.yaml (chart mode only)")
    parser.add_argument("--file", metavar="CSV_PATH",
                        help="CSV to build from (csv mode only; default: data/songs.csv)")
    parser.add_argument("--offline", action="store_true",
                        help="csv mode only: skip the live metadata fetch and build straight "
                             "from the CSV's own last-saved values (rank/views won't be refreshed). "
                             "chart mode does this automatically if it can't reach the registry/YouTube.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max songs to process")
    parser.add_argument("--no-filter", dest="no_filter", action="store_true",
                        help="Skip MV duration/title filtering (search mode only)")
    parser.add_argument("--download-workers", type=int, default=6,
                        help="Concurrent yt-dlp downloads (default: 6)")
    parser.add_argument("--encode-workers", type=int, default=3,
                        help="Concurrent ffmpeg overlay encodes (default: 3)")
    parser.add_argument("--prompt", metavar="TEXT",
                        help="Extra instructions to append to this run's audit prompt "
                             "(audit mode only, e.g. --prompt \"only look at channel X\")")
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

        # Steps 1 and 3 are the only git operations against the data repo
        # in this whole command - see db.py's module docstring. Everything
        # between them (discovery, catalog, snapshot, the audit agent)
        # reads/writes the local working copy pull_to_local() just seeded,
        # at local-disk speed.
        print("Fetching current registry from the data repo...")
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

        print("Pushing updated registry to the data repo...")
        db.push_from_local()
        return

    if args.command == "decouple":
        from registry import decouple as decouple_mod

        genres = args.genre or [None]
        if len(genres) > 1:
            parser.error("decouple takes at most one --genre")
        decouple_mod.decouple(genre=genres[0], assume_yes=args.yes)
        print("Local working copy updated - not pushed. Review the result, then "
              "`python run.py push` when you're ready to publish it to the data repo.")
        return

    if args.command == "regroup":
        from registry import regroup as regroup_mod

        genres = args.genre or [None]
        if len(genres) > 1:
            parser.error("regroup takes at most one --genre")
        regroup_mod.regroup_all(genre=genres[0])
        print("Local working copy updated - not pushed. Review the result, then "
              "`python run.py push` when you're ready to publish it to the data repo.")
        return

    if args.command == "merge-versions":
        from registry import merge_versions

        genres = args.genre or [None]
        if len(genres) > 1:
            parser.error("merge-versions takes at most one --genre")
        merge_versions.merge_versions(genre=genres[0], assume_yes=args.yes)
        print("Local working copy updated - not pushed. Review the result, then "
              "`python run.py push` when you're ready to publish it to the data repo.")
        return

    if args.command == "audit":
        from registry import grouping_audit

        grouping_audit.run_manual_audit(extra_instructions=args.prompt)
        return

    if args.command == "pull":
        from registry import db

        print("Rebuilding the local working copy from the data repo's current state...")
        db.pull_to_local()
        print(f"  Local working copy updated at {db.LOCAL_WORKING_DB_PATH}")
        return

    if args.command == "push":
        from registry import db

        print("Publishing the local working copy's current state to the data repo...")
        db.push_from_local()
        print("  Done.")
        return

    ensure_yt_dlp_up_to_date()
    warn_if_pot_provider_down()

    if args.command == "csv":
        run_pipeline(
            limit=args.limit,
            download_workers=args.download_workers,
            encode_workers=args.encode_workers,
            csv_file=args.file,
            offline=args.offline,
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
