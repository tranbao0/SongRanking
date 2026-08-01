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
import random
import time

try:
    from google import genai
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

try:
    from google.genai import errors as genai_errors
except ImportError:
    genai_errors = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from shared import api_budget

MODEL = "models/gemini-3.5-flash"

# Chunking exists to bound how much one request has to reason about, but
# measurement says it costs far more than it protects. Benchmarked on
# gemini-3.5-flash over 140 titles with known-correct groupings, carrying a
# 60-song candidate list as a real sync does:
#
#   size   calls   input tok   billable tok   wrong merges   missed links
#     15      10      14,165         18,017              0        111/127
#     30       5       8,117         18,828              0        101/127
#     45       4       6,911         15,975              0         83/127
#     60       3       5,699         15,248              0         76/127
#     90       2       4,490         13,156              0         49/127
#    140       1       3,323         15,496              0          0/127
#
# Two things drive this. The instruction preamble, the candidate list and a
# large fixed thinking cost are all paid per call, so input tokens fall ~60%
# from 15 to 60. And chunking is itself the main source of *wrong* output:
# two uploads of one song can only be merged if they land in the same chunk,
# so smaller chunks miss more real groupings. No mis-merge was observed at
# any size once the prompt was made explicit about re-arranged versions.
#
# 60 rather than the 140 the numbers alone would argue for, because a chunk
# is the blast radius of a single failed call: song_grouping leaves a failed
# chunk's videos ungrouped, and grouping is additive, so they are never
# revisited. 60 keeps most of the saving without putting an entire channel
# on one response.
CHUNK_SIZE = 60

# Retried rather than surfaced, because song_grouping treats a failed call
# as "leave these ungrouped" and grouping is additive - an ungrouped video
# is never revisited, so a transient 429 would permanently split a song
# instead of merely delaying it.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0


def chunked(items: list, size: int = CHUNK_SIZE) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _is_retryable(exc: Exception) -> bool:
    """
    True for rate limits and transient server-side failures only. A bad key
    or a malformed request is retried nowhere - each attempt is a real
    billed request, so retrying something that can't succeed just burns
    budget three times over.
    """
    if genai_errors is not None:
        if isinstance(exc, genai_errors.ServerError):
            return True
        if isinstance(exc, genai_errors.ClientError):
            return getattr(exc, "code", None) == 429
    text = str(exc).lower()
    return any(s in text for s in ("429", "rate limit", "resource_exhausted", "unavailable",
                                   "deadline", "timeout", "internal error", "503", "500"))


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

    for attempt in range(_MAX_ATTEMPTS):
        # Recorded per attempt, not per call: a retry is another billed
        # request and has to count against the day's budget as one.
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
            if attempt == _MAX_ATTEMPTS - 1 or not _is_retryable(e):
                print(f"  [gemini_client] Call failed ({e}).")
                return None
            # Jittered, so parallel callers that hit the same rate limit
            # don't all come back at the same instant and trip it again.
            delay = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1.0)
            print(f"  [gemini_client] Call failed ({e}) - retrying in {delay:.1f}s")
            time.sleep(delay)

    return None
