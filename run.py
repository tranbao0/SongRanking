"""
Command runner — cross-platform replacement for the Makefile.

Usage:
  python run.py update
  python run.py csv
  python run.py csv --limit 10
  python run.py search
  python run.py search --q "blackpink songs"
  python run.py search --q "blackpink songs" --limit 10
"""

import subprocess
import sys
import argparse


def cmd(args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="SongRanking command runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        choices=["update", "csv", "search"],
        help="Command to run",
    )
    parser.add_argument("--q", metavar="QUERY", default="kpop songs",
                        help='Search query (search mode only, default: "kpop songs")')
    parser.add_argument("--limit", type=int, default=None,
                        help="Max songs to process")
    args = parser.parse_args()

    if args.command == "update":
        cmd([sys.executable, "-m", "pip", "install", "-U", "-r", "requirements.txt"])

    elif args.command == "csv":
        pipeline_args = [sys.executable, "src/pipeline.py"]
        if args.limit:
            pipeline_args += ["--limit", str(args.limit)]
        cmd(pipeline_args)

    elif args.command == "search":
        pipeline_args = [sys.executable, "src/pipeline.py", "--search", args.q]
        if args.limit:
            pipeline_args += ["--limit", str(args.limit)]
        cmd(pipeline_args)


if __name__ == "__main__":
    main()
