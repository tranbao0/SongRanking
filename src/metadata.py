import subprocess
import json
from datetime import date


def fetch_metadata(url):
    """Return view count, release year, and years-on-chart from a YouTube URL."""
    result = subprocess.run(
        ["yt-dlp", "--dump-json", "--no-playlist", url],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)

    views = data.get("view_count") or 0

    upload_date = data.get("upload_date", "")   # YYYYMMDD string
    if len(upload_date) >= 4:
        release_year = int(upload_date[:4])
    else:
        release_year = date.today().year

    # Count the release year itself as year 1 on chart
    years_on_chart = max(1, date.today().year - release_year + 1)

    return {
        "views": views,
        "release_year": release_year,
        "years_on_chart": years_on_chart,
    }


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=IHNzOHi8sJs"
    import json as _json
    print(_json.dumps(fetch_metadata(url), indent=2))
