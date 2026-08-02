"""
AI-powered title cleanup for YouTube search results using Google Gemini.

Sends raw yt-dlp titles and uploader names to Gemini and gets back
short, display-ready song titles and artist names for the overlay.

Requires GEMINI_API_KEY in .env (or environment).
If the key is missing or a call fails, original titles are kept unchanged
for whichever chunk failed - chunks are independent, so one bad response
doesn't lose the whole batch.
"""

import concurrent.futures
import json

from shared import gemini_client
from shared.gemini_client import call_gemini, chunked

# Chunks are independent - each cleans a disjoint slice of `songs` in place -
# so running them concurrently only hides each call's network latency.
# Capped the same as song_grouping's AI chunking so a large chart doesn't
# open a burst wide enough to trip a rate limit.
_CHUNK_WORKERS = 4

_PROMPT_TEMPLATE = """\
You clean up YouTube video metadata for a K-pop song ranking overlay.
Rules:
- Song title: keep only the actual song name. Remove suffixes like "Official MV", \
"M/V", "Official Music Video", "Official Video", "Official Lyric Video", \
"Lyric Video", "Performance Video", "Dance Practice", and anything after a "|" pipe.
- Artist name: keep only the group or artist name. Remove label prefixes/suffixes \
like "HYBE LABELS", "HYBE LABELS and", "JYP Entertainment", "SM Entertainment", \
"YG Entertainment", "Big Hit Music", and similar. Remove "Official" or "and X more".
- Keep Korean characters exactly as-is.
- If you cannot determine a clean title, return the original unchanged.
- Be particularly careful with upload channel name versus artist name; if the artist is fictional, KEEP THE ARTIST NAME
Return ONLY a JSON array - no markdown, no explanation. Each element: {{"title": "...", "artist": "..."}}.

{entries}"""


def _clean_chunk(songs: list[dict]) -> None:
    """Clean one chunk's titles/artists in place. Leaves them unchanged on any failure."""
    entries = "\n".join(
        f"{i + 1}. title: {s['title']} | artist: {s['artist']}"
        for i, s in enumerate(songs)
    )

    text = call_gemini(_PROMPT_TEMPLATE.format(entries=entries))
    if text is None:
        return

    try:
        cleaned = json.loads(text)
        if not isinstance(cleaned, list) or len(cleaned) != len(songs):
            raise ValueError("Response length mismatch")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [title_cleaner] Chunk cleanup failed ({e}) - using original titles.")
        return

    for song, item in zip(songs, cleaned):
        if item.get("title"):
            song["title"] = item["title"].strip()
        if item.get("artist"):
            song["artist"] = item["artist"].strip()


def clean_titles(songs: list[dict]) -> list[dict]:
    """
    Call Gemini to clean up raw YouTube titles and uploader names, in
    small chunks rather than one large request (keeps each prompt
    focused, and one bad/failed chunk doesn't affect the rest). Chunks
    run concurrently since they're independent - see _CHUNK_WORKERS.

    Modifies the 'title' and 'artist' keys in-place on each song dict.
    Returns the same list (modified).
    """
    if not gemini_client.is_available():
        return songs

    chunks = chunked(songs)
    if len(chunks) <= 1:
        for chunk in chunks:
            _clean_chunk(chunk)
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_CHUNK_WORKERS, len(chunks))
        ) as pool:
            list(pool.map(_clean_chunk, chunks))

    print(f"  [title_cleaner] Cleaned {len(songs)} title(s) via Gemini.\n")
    return songs
