"""
Shared Gemini calling helper: client setup, budget accounting, response
extraction, and graceful failure handling in one place, plus a chunking
helper so callers send small, focused prompts instead of one large list
(better matching quality, less hallucination risk on big inputs).

Used by title_cleaner.py (title/artist cleanup) and song_grouping.py
(same-song clustering) - both need identical boilerplate around an
otherwise different prompt/response shape.
"""

import os

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

from shared import api_budget

MODEL = "models/gemini-3.5-flash"
CHUNK_SIZE = 15


def chunked(items: list, size: int = CHUNK_SIZE) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


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


def call_gemini(prompt: str, model: str = MODEL) -> str | None:
    """
    Send `prompt` to Gemini and return the raw response text (markdown
    code fences stripped), or None if the call can't be made or fails for
    any reason (SDK not installed, no API key, budget exhausted, API
    error) - callers should fall back to their pre-AI behavior on None.
    """
    if not _SDK_AVAILABLE:
        print("  [gemini_client] 'google-genai' package not installed - skipping AI call.")
        return None

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("  [gemini_client] GEMINI_API_KEY not set - skipping AI call.")
        return None

    try:
        api_budget.record_gemini_request()
    except api_budget.QuotaExceededError as e:
        print(f"  [gemini_client] {e}")
        return None

    try:
        client = genai.Client(api_key=api_key)
        interaction = client.interactions.create(
            model=model,
            input=prompt,
            generation_config={"max_output_tokens": 65536},
        )
        text = _extract_text(interaction.steps[-1]).strip()

        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.startswith("```")
            ).strip()
        return text

    except Exception as e:
        print(f"  [gemini_client] Call failed ({e}).")
        return None
