import concurrent.futures
import subprocess
import json
from datetime import date

from shared.dates import months_since


def fetch_metadata(url):
    """Return view count, release date/year, and months-on-chart from a YouTube URL."""
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
        "release_year": release_year,
        "release_date": release_date.strftime("%Y.%m.%d"),
        "years_on_chart": years_on_chart,
        "months_on_chart": months_on_chart,
    }


def batch_fetch_metadata(urls, max_workers: int = 8) -> dict:
    """
    Fetch metadata for many videos concurrently via yt-dlp.

    yt-dlp has no batch endpoint, so each URL is still its own subprocess —
    but running them on a thread pool instead of one at a time means the
    wall-clock cost is ~len(urls)/max_workers subprocess calls instead of
    len(urls), which matters once lists run into the hundreds.

    Returns {url: meta_dict}. URLs that fail to fetch are omitted (a
    warning is printed for each).
    """
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_url = {pool.submit(fetch_metadata, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                results[url] = future.result()
            except subprocess.CalledProcessError as e:
                print(f"    WARNING: metadata fetch failed for {url} ({e}), skipping.")
    return results


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=IHNzOHi8sJs"
    import json as _json
    print(_json.dumps(fetch_metadata(url), indent=2))
