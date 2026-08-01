"""
kworb-based channel discovery. kworb publishes per-country weekly YouTube
video charts (no channel links/IDs — just "Artist - Track" text), so this
is a popularity-based seed, not an authoritative genre roster: a country's
chart mixes in artists who don't belong to the target genre (e.g. a K-pop
act charting on Japan's page). Callers should treat this as additive on
top of providers.wikidata, and prune false positives via each genre's
exclude file (see discovery.py).

Resolution to a channel ID is done via yt-dlp rather than the YouTube Data
API: search.list costs 100 quota units per call, which is prohibitive
against a ~100-row chart page (100 rows would alone consume the entire
default daily quota). yt-dlp search costs no API quota at all, and
matching on the full "artist + track" pair (an actual charting video) is
also more precise than an artist-name-only channel search would be, since
it pins down the real official upload rather than guessing between
VEVO/fan/topic channels that share the artist's name.
"""

import concurrent.futures
import subprocess
from datetime import date

import requests
from bs4 import BeautifulSoup

CHART_URL = "https://kworb.net/youtube/insights/{country}.html"

# One representative country chart per genre, used purely as a popularity
# seed — not a claim that the genre is exclusive to that market.
GENRE_COUNTRIES = {
    "kpop": "kr",
    "jpop": "jp",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SongRankingBot/1.0)"}
_RESOLVE_WORKERS = 8


def _fetch_chart_entries(country: str) -> list[tuple[str, str]]:
    """Return deduped [(artist, track), ...] from a kworb country chart page."""
    response = requests.get(CHART_URL.format(country=country), headers=_HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    entries = []
    seen_artists = set()
    for cell in soup.select("table#weeklytable tbody td.text.mp div"):
        text = cell.get_text(strip=True)
        if " - " not in text:
            continue
        artist, track = text.split(" - ", 1)
        artist = artist.strip()
        if artist and artist not in seen_artists:
            seen_artists.add(artist)
            entries.append((artist, track.strip()))
    return entries


def _resolve_channel(artist: str, track: str) -> tuple[str, str] | None:
    """
    Resolve an (artist, track) pair to (channel_id, channel_name) via a
    yt-dlp search for that exact charting video. Deliberately not
    --flat-playlist: flat search results don't reliably populate
    channel_id (confirmed empirically — it comes back "NA" often enough
    to matter), so this pays for a full single-video extraction per
    candidate instead. Returns None if yt-dlp finds nothing or the
    result has no channel_id (e.g. a non-YouTube upload got matched).
    """
    result = subprocess.run(
        [
            "yt-dlp", f"ytsearch1:{artist} {track}",
            "--no-warnings",
            "--print", "%(channel_id)s|||%(channel)s",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    parts = line[0].split("|||")
    if len(parts) != 2 or not parts[0] or parts[0] == "NA":
        return None
    return parts[0].strip(), parts[1].strip()


def discover_channels(genre: str) -> list[dict]:
    """
    Return channel entries for `genre` seeded from kworb's chart for that
    genre's representative country. Entries that fail to resolve to a
    channel are silently dropped.
    """
    country = GENRE_COUNTRIES.get(genre)
    if country is None:
        return []

    entries = _fetch_chart_entries(country)
    channels = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_RESOLVE_WORKERS) as pool:
        futures = {pool.submit(_resolve_channel, artist, track): artist for artist, track in entries}
        for future in concurrent.futures.as_completed(futures):
            resolved = future.result()
            if resolved is None:
                continue
            channel_id, channel_name = resolved
            channels.append({
                "channel_id":   channel_id,
                "genre":        genre,
                "display_name": channel_name,
                "source":       "kworb",
                "source_ref":   None,
                "added_date":   date.today().isoformat(),
            })
    return channels
