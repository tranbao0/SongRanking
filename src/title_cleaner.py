"""
AI-powered title cleanup for YouTube search results using Google Gemini.

Sends raw yt-dlp titles and uploader names to Gemini and gets back
short, display-ready song titles and artist names for the overlay.

Requires GEMINI_API_KEY in .env (or environment).
If the key is missing or the call fails, original titles are kept unchanged.
"""

import os
import json

try:
    from google import genai
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


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
Return ONLY a JSON array — no markdown, no explanation. Each element: {{"title": "...", "artist": "..."}}.

{entries}"""


def _extract_text(step) -> str:
    """Pull plain text out of a Gemini interaction step."""
    if hasattr(step, "text"):
        return step.text
    if hasattr(step, "content"):
        content = step.content
        if isinstance(content, str):
            return content
        if isinstance(content, list) and content:
            part = content[0]
            return part.text if hasattr(part, "text") else str(part)
    return str(step)


def clean_titles(songs: list[dict]) -> list[dict]:
    """
    Call Gemini to clean up raw YouTube titles and uploader names.

    Modifies the 'title' and 'artist' keys in-place on each song dict.
    Returns the same list (modified).
    Silently skips cleanup if the API key is absent or the call fails.
    """
    if not _SDK_AVAILABLE:
        print("  [title_cleaner] 'google-genai' package not installed — skipping AI cleanup.")
        return songs

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("  [title_cleaner] GEMINI_API_KEY not set — skipping AI cleanup.")
        return songs

    entries = "\n".join(
        f"{i + 1}. title: {s['title']} | artist: {s['artist']}"
        for i, s in enumerate(songs)
    )

    try:
        client = genai.Client(api_key=api_key)

        generation_config = {
            "max_output_tokens": 65536,
        }

        interaction = client.interactions.create(
            model="models/gemini-3.5-flash",
            input=_PROMPT_TEMPLATE.format(entries=entries),
            generation_config=generation_config,
        )

        text = _extract_text(interaction.steps[-1]).strip()

        # Strip markdown code fences if the model wraps its response
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines()
                if not line.startswith("```")
            ).strip()

        cleaned = json.loads(text)

        if not isinstance(cleaned, list) or len(cleaned) != len(songs):
            raise ValueError("Response length mismatch")

        for song, item in zip(songs, cleaned):
            if item.get("title"):
                song["title"] = item["title"].strip()
            if item.get("artist"):
                song["artist"] = item["artist"].strip()

        print(f"  [title_cleaner] Cleaned {len(songs)} title(s) via Gemini.\n")

    except Exception as e:
        print(f"  [title_cleaner] Cleanup failed ({e}) — using original titles.\n")

    return songs
