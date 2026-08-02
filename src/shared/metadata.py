import concurrent.futures
import os
import re
import subprocess
import threading
import time
import json
from datetime import date

from shared.dates import months_since

# yt-dlp's fatal line looks like "ERROR: [youtube] <video_id>: <reason>" -
# the ID makes every video's message unique text, which would otherwise
# defeat grouping identical failures (e.g. a bot-check wall hit by
# thousands of videos in the same run) into one line.
_ERROR_ID_PREFIX_RE = re.compile(r"^ERROR:\s*\[\w+\]\s*[\w-]+:\s*")

# Firing many yt-dlp requests at once is what actually trips YouTube's
# bot-check wall in practice - a burst against a large pending list came
# back 429 ("Too Many Requests") then "Sign in to confirm you're not a
# bot" on nearly every video. Unlike the Data API, yt-dlp has no quota to
# negotiate against, only YouTube's own request-rate heuristics, so the
# fix is pacing rather than budgeting: every launch waits for this long
# to have passed since the last one, no matter how many worker threads
# are free to fire immediately. Configurable because the right interval
# depends on how aggressively YouTube is currently rate-limiting, which
# isn't something this codebase can observe directly.
_MIN_LAUNCH_INTERVAL = float(os.environ.get("YTDLP_MIN_REQUEST_INTERVAL", 1.5))
_launch_lock = threading.Lock()
_last_launch = 0.0


def _throttle() -> None:
    """Block until at least _MIN_LAUNCH_INTERVAL has passed since the last
    yt-dlp launch (across every thread), then claim this slot."""
    global _last_launch
    with _launch_lock:
        wait = _last_launch + _MIN_LAUNCH_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_launch = time.monotonic()


def _error_reason(e: subprocess.CalledProcessError) -> str:
    """The last ERROR line of yt-dlp's stderr, with the per-video ID
    stripped so identical failures group together - falls back to
    whatever the process printed last, or str(e), if there's no ERROR
    line to find (e.g. it was killed rather than exiting cleanly)."""
    lines = [ln.strip() for ln in (e.stderr or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            return _ERROR_ID_PREFIX_RE.sub("", line)
    return lines[-1] if lines else str(e)


def fetch_metadata(url):
    """Return view count, duration, release date/year, and months-on-chart from a YouTube URL."""
    _throttle()
    result = subprocess.run(
        ["yt-dlp", "--dump-json", "--no-playlist", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    data = json.loads(result.stdout)

    views = data.get("view_count") or 0

    upload_date = data.get("upload_date", "")   # YYYYMMDD string
    if len(upload_date) == 8:
        release_date = date(int(upload_date[:4]), int(upload_date[4:6]), int(upload_date[6:8]))
    else:
        release_date = date.today()
    release_year = release_date.year

    # Count the release year itself as year 1 on chart
    years_on_chart = max(1, date.today().year - release_year + 1)
    months_on_chart = months_since(release_date)

    return {
        "views": views,
        "duration": int(data.get("duration") or 0),
        "release_year": release_year,
        "release_date": release_date.strftime("%Y.%m.%d"),
        "years_on_chart": years_on_chart,
        "months_on_chart": months_on_chart,
    }


def batch_fetch_metadata(urls, max_workers: int = 8, on_result=None) -> dict:
    """
    Fetch metadata for many videos concurrently via yt-dlp.

    yt-dlp has no batch endpoint, so each URL is still its own subprocess -
    but running them on a thread pool instead of one at a time means the
    wall-clock cost is ~len(urls)/max_workers subprocess calls instead of
    len(urls), which matters once lists run into the hundreds. Actual
    launches are paced by _throttle() regardless of max_workers, so a
    higher worker count buys overlap on each call's network wait rather
    than a higher burst rate.

    If given, on_result(url, meta) is called (on this thread) as each
    video finishes, before this function returns - lets a caller like
    snapshot.take_snapshot persist progress as it arrives instead of
    losing an entire long, throttle-paced batch if the run is killed
    partway through.

    Returns {url: meta_dict}. URLs that fail to fetch are omitted, and
    reported once as a single grouped summary at the end (by distinct
    failure reason) rather than one line per video - a systemic failure
    like a bot-check wall hits every video in the batch identically, and
    a thousand copies of the same line would drown out everything else.
    """
    total = len(urls)
    # Only large batches need a progress trickle - without one, a bulk
    # fallback (thousands of videos, each throttled to ~1 request/second)
    # can run silently for a long time and look hung.
    log_interval = max(1, total // 20)

    results = {}
    failures: dict[str, int] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_url = {pool.submit(fetch_metadata, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                meta = future.result()
                results[url] = meta
                if on_result:
                    on_result(url, meta)
            except subprocess.CalledProcessError as e:
                reason = _error_reason(e)
                failures[reason] = failures.get(reason, 0) + 1
            done += 1
            if total > 40 and done % log_interval == 0:
                print(f"    ...{done}/{total} video(s) processed via yt-dlp")

    if failures:
        failed_total = sum(failures.values())
        print(f"    WARNING: {failed_total}/{total} video(s) failed via yt-dlp, skipped. Reason(s):")
        for reason, count in sorted(failures.items(), key=lambda kv: -kv[1]):
            print(f"      {count}x {reason}")

    print(f"    Fetched metadata for {len(results)}/{total} video(s) via yt-dlp.")
    return results


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=IHNzOHi8sJs"
    import json as _json
    print(_json.dumps(fetch_metadata(url), indent=2))
