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
import threading
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

# Interactions API model id (Aug 2026). 3.6 Flash matches 3.5 Flash on
# input price ($1.50/1M) but is cheaper on output including thinking
# ($7.50/1M vs $9.00/1M) and typically emits fewer thinking tokens.
MODEL = "gemini-3.6-flash"

# Default for this project's callers (grouping + title cleanup), which are
# classification/extraction - not agent loops. Google recommends low/minimal
# for that class of work; at medium, thinking often dominates the output bill.
# Callers can still raise it per request.
THINKING_LEVEL = "low"

# Cap for thinking + final text combined on Gemini 3.x. 65k invited the
# model to spend a huge thinking budget on short JSON answers; a few
# thousand is enough for a CHUNK_SIZE grouping/cleanup response while
# still leaving room for low/medium thinking.
MAX_OUTPUT_TOKENS = 8192

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
# chunk's videos with song_id NULL for regroup. Smaller chunks limit how
# many titles one outage delays, without putting an entire channel on one
# response.
CHUNK_SIZE = 60

# Retried rather than surfaced as a hard error, because a failed call used
# to mint locked singletons. Sync now leaves those videos with song_id
# NULL instead, and regroup can finish them - but a transient 429 that
# isn't retried still costs a wasted attempt and delays grouping, so
# transport retries remain worthwhile.
#
# Patience is sized for the failure seen in practice, which is not a brief
# blip: a 500 "currently experiencing high demand" on a Flash model can
# persist for minutes. Four attempts backing off 4s/8s/16s wait roughly
# half a minute, where 3 attempts at 2s/4s gave up after six seconds and
# stranded the chunk. Bounded rather than generous because every attempt
# is counted against the day's budget, so a sustained outage must not
# spend the whole allowance on retries.
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 4.0

# One Client per process/api key. Creating a new Client on every attempt
# (and every parallel chunk) paid setup cost for no benefit - the SDK is
# safe to share across threads for independent interactions.create calls.
_CLIENT = None
_CLIENT_KEY = None
_CLIENT_LOCK = threading.Lock()


def chunked(items: list, size: int = CHUNK_SIZE) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# Reported once per process rather than once per call. Running deliberately
# without a key is a supported mode - the free grouping tiers do the bulk of
# the work - and a full sync makes thousands of calls, so repeating the
# reason for each would bury everything else in the log.
_UNAVAILABLE_REPORTED = False

# Same reasoning as _UNAVAILABLE_REPORTED: once the daily budget is gone,
# every remaining chunk in the run hits it the same way, so this is
# reported once rather than once per chunk.
_QUOTA_REPORTED = False


def is_available() -> bool:
    """
    Whether an AI call could be made at all: SDK importable and a key set.

    Callers check this *before* preparing work, so a run without a key
    skips the AI tier outright instead of building every prompt and
    throwing the result away. call_gemini re-checks independently, so this
    is an optimisation and a cleaner log, never the safety barrier.
    """
    global _UNAVAILABLE_REPORTED
    if not _SDK_AVAILABLE:
        reason = "'google-genai' package not installed"
    elif not os.environ.get("GEMINI_API_KEY", "").strip():
        reason = "GEMINI_API_KEY not set"
    else:
        return True

    if not _UNAVAILABLE_REPORTED:
        print(f"  [gemini_client] {reason} - AI steps are disabled for this run "
              f"(grouping still runs its free tiers; unmatched uploads stay their own song).")
        _UNAVAILABLE_REPORTED = True
    return False


def _get_client(api_key: str):
    """Return a process-wide Client for `api_key`, creating it once."""
    global _CLIENT, _CLIENT_KEY
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_KEY != api_key:
            _CLIENT = genai.Client(api_key=api_key)
            _CLIENT_KEY = api_key
        return _CLIENT


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


def _extract_text(interaction) -> str:
    """
    Pull plain text out of an Interactions response.

    Prefer `.output_text` (SDK convenience for the final model text) when
    present; fall back to the last step for older response shapes used in
    tests and for responses where convenience properties are empty.
    """
    text = getattr(interaction, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    steps = getattr(interaction, "steps", None) or []
    if not steps:
        return ""
    step = steps[-1]
    if hasattr(step, "text") and step.text:
        return step.text
    if hasattr(step, "content"):
        content = step.content
        if isinstance(content, str):
            return content
        if isinstance(content, list) and content:
            part = content[0]
            return part.text if hasattr(part, "text") else str(part)
    return str(step)


def call_gemini(
    prompt: str,
    model: str = MODEL,
    response_schema: dict | None = None,
    thinking_level: str | None = THINKING_LEVEL,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> str | None:
    """
    Send `prompt` to Gemini and return the raw response text (markdown
    code fences stripped), or None if the call can't be made or fails for
    any reason (SDK not installed, no API key, budget exhausted, API
    error) - callers should fall back to their pre-AI behavior on None.

    `response_schema` is an optional JSON Schema passed through
    Interactions `response_format`, so the API enforces structured JSON
    instead of relying on prompt wording. `thinking_level` maps to
    generation_config (Gemini 3.x); pass None to leave the model default.
    """
    if not _SDK_AVAILABLE:
        print("  [gemini_client] 'google-genai' package not installed - skipping AI call.")
        return None

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("  [gemini_client] GEMINI_API_KEY not set - skipping AI call.")
        return None

    generation_config: dict = {"max_output_tokens": max_output_tokens}
    if thinking_level:
        generation_config["thinking_level"] = thinking_level

    create_kwargs: dict = {
        "model": model,
        "input": prompt,
        "generation_config": generation_config,
    }
    if response_schema is not None:
        # Interactions API (May 2026+): mime_type + schema live on
        # response_format, not generation_config.
        create_kwargs["response_format"] = {
            "type": "text",
            "mime_type": "application/json",
            "schema": response_schema,
        }

    for attempt in range(_MAX_ATTEMPTS):
        # Recorded per attempt, not per call: a retry is another billed
        # request and has to count against the day's budget as one.
        try:
            api_budget.record_gemini_request()
        except api_budget.QuotaExceededError as e:
            global _QUOTA_REPORTED
            if not _QUOTA_REPORTED:
                print(f"  [gemini_client] {e}")
                _QUOTA_REPORTED = True
            return None

        try:
            client = _get_client(api_key)
            interaction = client.interactions.create(**create_kwargs)
            text = _extract_text(interaction).strip()

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
