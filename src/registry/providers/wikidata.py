"""
Wikidata SPARQL-based channel discovery. Authoritative source: any artist
Wikidata tags with the target genre and links to a YouTube channel (P2397)
is included. This is the only automated discovery source; label and
distributor channels, which have no artist entry here, are curated in
data/channels/<genre>_manual.yaml instead.

Deliberately does not filter on "instance of" (P31) - restricting to
"musical group" would exclude solo artists (e.g. PSY), so the only
requirement is the genre tag itself.
"""

import random
import re
import time
from datetime import date

import requests

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# A transient blip (timeout, connection reset, a 5xx) shouldn't cost an
# entire sync's channel discovery for every genre - see discover_channels.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0

# One YouTube channel ID (P2397) per artist is typical; genre (P136) subclass
# reasoning (P279*) catches artists tagged with a more specific sub-genre.
_QUERY = """
SELECT ?artist ?artistLabel ?youtubeChannel WHERE {{
  ?artist wdt:P136/wdt:P279* wd:{genre_qid} .
  ?artist wdt:P2397 ?youtubeChannel .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""

# Wikidata's usage policy asks for a descriptive User-Agent identifying the
# tool and a contact point, rather than a generic client string.
_HEADERS = {
    "User-Agent": "SongRankingBot/1.0 (https://github.com/tranbao0/SongRanking)",
    "Accept": "application/sparql-results+json",
}

GENRE_QIDS = {
    "kpop": "Q213665",
    "jpop": "Q131578",
}

_QID_RE = re.compile(r"Q\d+$")


def _query_bindings(genre_qid: str) -> list[dict]:
    """
    Run the SPARQL query, retrying a transient failure (timeout, connection
    error, a 5xx) up to _MAX_ATTEMPTS times before letting it propagate.
    Raises requests.RequestException if every attempt fails - the caller
    (discovery.sync_channels) decides how to treat one genre's discovery
    failing without taking the rest of the run down with it.
    """
    last_exc: requests.RequestException | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                SPARQL_ENDPOINT,
                params={"query": _QUERY.format(genre_qid=genre_qid), "format": "json"},
                headers=_HEADERS,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()["results"]["bindings"]
        except requests.RequestException as e:
            last_exc = e
            if attempt == _MAX_ATTEMPTS - 1:
                break
            delay = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1.0)
            print(f"  [wikidata] Query failed ({e}) - retrying in {delay:.1f}s")
            time.sleep(delay)
    raise last_exc


def discover_channels(genre: str) -> list[dict]:
    """
    Return channel entries for `genre` found via Wikidata.
    Each entry: {channel_id, genre, display_name, source, source_ref, added_date}
    """
    genre_qid = GENRE_QIDS.get(genre)
    if genre_qid is None:
        return []

    bindings = _query_bindings(genre_qid)

    channels = []
    for row in bindings:
        channel_id = row.get("youtubeChannel", {}).get("value", "").strip()
        if not channel_id:
            continue
        artist_uri = row.get("artist", {}).get("value", "")
        qid_match = _QID_RE.search(artist_uri)
        channels.append({
            "channel_id":   channel_id,
            "genre":        genre,
            "display_name": row.get("artistLabel", {}).get("value", channel_id),
            "source":       "wikidata",
            "source_ref":   qid_match.group(0) if qid_match else None,
            "added_date":   date.today().isoformat(),
        })
    return channels
