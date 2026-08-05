"""
Hands the registry off to a subscription-based CLI coding agent (Claude
Code, or whatever the operator has configured) for a song-grouping
quality audit after a sync.

Why this exists: song_grouping.py's free/AI tiers handle the common case
cheaply, but structurally can't do everything - the AI tier only ever
sees one chunk of one channel at a time, so it can't catch a duplicate
uploaded to a different channel (see docs/manual-grouping-prompt.md's
"cross-channel duplicate detection"), and neither tier carries the
dozens of judgment-call precedents (Ver.-tag semantics, medley
ambiguity, native-script identity, Making/Behind content that doesn't
use the word "Behind") that only came out of actually reading titles
across many audits. Measured on this project: a fresh batch of
never-reviewed channels needed roughly one correction per eight new
uploads; the same channels' next incremental sync needed one correction
in 27,444 videos. That gap is exactly what this module targets, and only
where it's likely to be big enough to matter.

Three independent triggers, any one of which is enough - each is decided
from data already in the registry, no agent call needed to check:

- A brand-new channel appeared this sync. Its whole catalog needs a
  first read, which is the highest-value case there is (see the
  measurement above).
- A single channel's new-upload count this sync crosses
  VOLUME_THRESHOLD. An *existing*, already-reviewed channel dumping a
  large batch at once (e.g. its full back-catalog of old concert clips)
  carries the same judgment load as a new channel without technically
  being one - a fixed run-count alone would miss this until the next
  scheduled audit.
- AUDIT_EVERY_N_SYNCS have passed since the last audit, regardless of
  volume. A low-stakes backstop: a missed cross-channel duplicate just
  sits as a minor chart inaccuracy until caught, it never corrupts
  anything, because view_snapshots keys on video_id and a chart
  re-aggregates through videos.song_id at render time - so correcting a
  grouping mistake late costs nothing already collected.

The agent runs headless (prompt piped over stdin, one-shot, no
back-and-forth possible) with direct write access to the local working
copy sync already pulled from the hosted registry (db.LOCAL_WORKING_DB_PATH)
- it applies its own fixes rather than proposing a diff for a human, since
this is a lone unattended pass with no concurrent writer to race. Runs
between sync's own pull and push (see run.py), so its edits are part of
what gets pushed back at the end. Launched with permission checks
bypassed (see AGENT_CLI_COMMAND) - headless mode has no human to answer
a permission prompt, so without that flag every edit the agent tries
would be silently rejected and the audit would be a no-op.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

from registry import db

_ROOT = Path(__file__).parent.parent.parent
STATE_FILE = _ROOT / "data" / ".grouping_audit_state.json"
LOG_FILE = _ROOT / "data" / ".grouping_audit_log.jsonl"
METHODOLOGY_DOC = "docs/manual-grouping-prompt.md"
PROGRESS_FILE = "data/.manual-grouping-progress.json"

# The agent CLI to invoke, as argv tokens - not hardcoded to one vendor.
# Point this at whatever subscription-based coding-agent CLI is
# installed and authenticated; the only requirement is that it accepts a
# one-shot prompt on stdin and can read/write files in the given cwd.
# Default matches Claude Code's headless/print mode:
#   --permission-mode bypassPermissions - this is a one-shot run with no
#     human able to answer a permission prompt (see the module
#     docstring), so without this every Edit/Write/Bash call the agent
#     makes is silently rejected and the audit does nothing at all.
#   --verbose --output-format stream-json - emits one JSON line per
#     turn (assistant text, tool calls, tool results) as they happen,
#     which _invoke_agent parses into human-readable progress lines
#     instead of only learning the outcome after the whole run finishes.
# A custom GROUPING_AGENT_CLI that doesn't speak stream-json still works
# - _print_progress_event falls back to echoing unparsable lines as-is.
AGENT_CLI_COMMAND = os.environ.get(
    "GROUPING_AGENT_CLI",
    "claude -p --verbose --output-format stream-json --permission-mode bypassPermissions",
).split()


AUDIT_EVERY_N_SYNCS = int(os.environ.get("GROUPING_AUDIT_EVERY_N_SYNCS", 7))

# A single channel's new-upload count in one sync that's treated as
# carrying the same judgment load as a brand-new channel. Tuned from
# this project's own history: a channel that actually needs review
# arrives with dozens to low hundreds of new uploads at once; routine
# incremental syncs of an already-reviewed channel are usually single
# digits (see the module docstring's measurement).
VOLUME_THRESHOLD = int(os.environ.get("GROUPING_AUDIT_VOLUME_THRESHOLD", 50))

# Generous on purpose: an audit reads and judges titles rather than
# calling an API, and a large trigger (a new label channel with a
# thousand-video back-catalogue) can legitimately take a while.
AGENT_TIMEOUT_SECONDS = int(os.environ.get("GROUPING_AGENT_TIMEOUT", 3600))


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"syncs_since_audit": 0}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def snapshot_before_sync() -> dict:
    """
    Call before discovery/catalog syncing starts. Captures just enough
    to diff against afterward - the channel_id set (to find brand-new
    channels) and a per-channel video count (to find volume spikes) -
    without depending on catalog.sync_videos' internals or return value.
    """
    conn = db.get_connection()
    try:
        channel_ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channels")}
        video_counts = {
            r["channel_id"]: r["c"]
            for r in conn.execute("SELECT channel_id, COUNT(*) c FROM videos GROUP BY channel_id")
        }
        return {"channel_ids": channel_ids, "video_counts": video_counts}
    finally:
        conn.close()


def _decide_trigger(before: dict) -> tuple[str, dict] | tuple[None, None]:
    """
    Compare the pre-sync snapshot to current state and return (reason,
    details) for the first trigger that fires, or (None, None) if none
    do. Checked in the order given in the module docstring: new channels
    first (cheapest signal, highest value), then volume, then the
    periodic backstop.
    """
    conn = db.get_connection()
    try:
        current_ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channels")}
        new_channel_ids = current_ids - before["channel_ids"]
        if new_channel_ids:
            rows = conn.execute(
                f"SELECT channel_id, display_name FROM channels "
                f"WHERE channel_id IN ({','.join('?' * len(new_channel_ids))})",
                list(new_channel_ids),
            ).fetchall()
            return "new_channels", {"channels": [(r["channel_id"], r["display_name"]) for r in rows]}

        current_counts = {
            r["channel_id"]: r["c"]
            for r in conn.execute("SELECT channel_id, COUNT(*) c FROM videos GROUP BY channel_id")
        }
        spikes = []
        for channel_id, after_count in current_counts.items():
            delta = after_count - before["video_counts"].get(channel_id, 0)
            if delta >= VOLUME_THRESHOLD:
                row = conn.execute(
                    "SELECT display_name FROM channels WHERE channel_id = ?", (channel_id,)
                ).fetchone()
                spikes.append((channel_id, row["display_name"] if row else channel_id, delta))
        if spikes:
            return "volume_spike", {"channels": spikes}

        state = _load_state()
        if state.get("syncs_since_audit", 0) + 1 >= AUDIT_EVERY_N_SYNCS:
            return "periodic", {}

        return None, None
    finally:
        conn.close()


def _build_prompt(reason: str, details: dict) -> str:
    if reason == "new_channels":
        listing = "\n".join(f"- {cid} ({name})" for cid, name in details["channels"])
        scope = (
            f"{len(details['channels'])} brand-new channel(s) were added to the registry this "
            f"sync and have never been reviewed:\n{listing}\n\n"
            "Review each one's full catalog (query through videos.channel_id) for duplicate "
            "songs uploaded under different presentation formats (MV/Dance Practice/Performance "
            "Video/Live Clip/etc. of the same song landing as separate `songs` rows), and for "
            "Making/Behind/Reaction/Commentary content that got a song row instead of being "
            "excised. If any of these channels is large (call it more than ~150 new videos), "
            "consider splitting the work across a few parallel Claude Code agents the way past "
            "large batches were handled (see the doc's precedent history) - for anything smaller "
            "a single pass is enough, don't spin up agents you don't need."
        )
    elif reason == "volume_spike":
        listing = "\n".join(f"- {cid} ({name}): {delta} new video(s)" for cid, name, delta in details["channels"])
        scope = (
            f"The following already-reviewed channel(s) received an unusually large batch of "
            f"new uploads in one sync (>= {VOLUME_THRESHOLD}), which often means a back-catalog "
            f"dump (e.g. old concert clips) rather than routine new releases:\n{listing}\n\n"
            "Review just the new uploads on these channels (videos.discovered_at is date-only in "
            "this schema, so cross-reference against songs.grouped_at, which has microsecond "
            "precision, to find the cutoff - or just check every song on the channel that has "
            "more than one video, since these channels were already clean before this sync) for "
            "duplicates that should merge into an existing song, and for Making/Behind/Reaction/"
            "Commentary content that should be excised instead of grouped."
        )
    else:
        scope = (
            f"This is the periodic backstop audit (runs every {AUDIT_EVERY_N_SYNCS} syncs "
            "regardless of volume, to catch what the two targeted triggers don't). Run the "
            "whole-registry normalize-and-cluster duplicate scan described in the doc under "
            "\"Do this by reading, not by writing matching code\" - it generates cross-channel "
            "duplicate candidates for you to read and confirm, which is the main category of "
            "miss a per-channel or per-sync trigger structurally can't catch. Confirm and apply "
            "genuine merges; when unsure, leave split per the doc's default."
        )

    return f"""You are doing an unattended song-grouping quality audit on the K-pop registry
at {db.LOCAL_WORKING_DB_PATH}, in the SongRanking project rooted at the current working
directory. This is a local SQLite file, not the hosted database directly - it's a working
copy this sync run pulled from the hosted registry and will push back once everything
(including this audit) is done, so editing it directly is exactly the right thing to do,
not a workaround. This is a one-shot, non-interactive run - there is no human to ask a
clarifying question, so make the conservative call (per the doc's "when unsure, don't
merge" rule) rather than waiting on anything.

Read {METHODOLOGY_DOC} in full before doing anything else. It has the complete decided
rules, the DO-NOT-merge list, the Making/Behind excision rule and its exact SQL patterns,
and a large "Precedents from prior runs" section with dozens of worked examples - apply
those rather than re-deriving them.

That doc is written primarily for a *separate*, human-run workflow (several manual
Claude Code instances each grouping one shard of the registry) - its "Before starting
any instance", "The prompt" (the literal SHARD/SHARD_COUNT template), and "After all
shards finish" sections each contain their own Turso backup/pull/push steps
(`scripts/turso_shell.sh`, `db.pull_to_local()`, `db.push_from_local()`) for that
workflow. None of that applies to you: do not run those commands, do not touch Turso in
any way, and do not pull or push the registry yourself. You are one unattended pass
over the local working copy only, exactly as it exists on disk right now - whether it
gets pulled before this run or pushed back after is entirely up to the process that
invoked you (a sync, or an operator running `python run.py audit`), never something you
do yourself. Skip those three sections; follow only the rules, precedents, and apply/
excise procedures.

Trigger and scope for this run:
{scope}

You have direct read/write access to {db.LOCAL_WORKING_DB_PATH} - apply merges and
excisions directly as you find them (see the doc's "How to apply a merge" / "How to
excise" sections), you don't need to produce a diff for a human to review since this is a
single unattended pass with nothing else writing to the database concurrently.

When done:
1. Run `DELETE FROM songs WHERE song_id NOT IN (SELECT DISTINCT song_id FROM videos WHERE song_id IS NOT NULL)` once, after all your fixes are applied.
2. Run `python -m unittest discover -s tests -t .` and confirm it's clean.
3. This is the step that makes future audits smarter instead of re-deriving the same
   judgment calls from scratch - don't skip it even on a clean run. If you had to make any
   judgment call not already covered by {METHODOLOGY_DOC}'s "Precedents from prior runs"
   section (a new Ver.-tag reading, a new non-song content shape, anything you weren't sure
   was covered), add it there directly, in that section's existing style - that file is
   what every future run (human or agent) actually reads as "the rules" before doing
   anything, so a precedent that only lives in a log gets rediscovered the hard way next
   time instead of applied. Don't touch anything above the "Precedents" heading - those are
   decided rules, not open for revision by an audit.
4. Separately, append a dated entry to {PROGRESS_FILE} (create it if missing, following the
   shape of the existing entries) recording what you reviewed and what you fixed - this is
   the tracking log, not the rulebook, so a summary is enough; the actual precedent belongs
   in the doc per step 3, not only here.
5. If you found nothing to fix, say so plainly in that entry rather than skipping it -
   a clean audit is itself useful information about how well the automated tiers are doing.
"""


def _summarize_tool_input(tool_input: dict) -> str:
    """
    One-line human summary of a tool_use block's input, for progress
    printing - the field that actually says what's happening (which
    file, what command) rather than the full argument dict.
    """
    for key in ("file_path", "command", "pattern", "query"):
        if key in tool_input:
            return str(tool_input[key])[:150]
    return json.dumps(tool_input)[:150]


def _print_progress_event(line: str) -> None:
    """
    Parse one line of `claude -p --output-format stream-json` output and
    print a short human-readable progress line. Falls back to echoing
    the raw line for anything that isn't JSON we recognize - keeps this
    useful even against a GROUPING_AGENT_CLI override that doesn't speak
    stream-json, since then every line just passes through as-is.
    """
    line = line.rstrip("\n")
    if not line.strip():
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        print(f"  [grouping_audit]   {line}", flush=True)
        return

    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        print(f"  [grouping_audit] agent session started (model {event.get('model')})", flush=True)
    elif kind == "system" and event.get("subtype") == "api_retry":
        print(f"  [grouping_audit]   (API retry {event.get('attempt')}/{event.get('max_retries')})", flush=True)
    elif kind == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                print(f"  [grouping_audit]   {block['text'].strip()[:300]}", flush=True)
            elif block.get("type") == "tool_use":
                print(f"  [grouping_audit]   -> {block.get('name')}({_summarize_tool_input(block.get('input', {}))})", flush=True)
    elif kind == "user":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_result" and block.get("is_error"):
                print(f"  [grouping_audit]      tool error: {str(block.get('content'))[:300]}", flush=True)
    elif kind == "result":
        turns = event.get("num_turns")
        cost = event.get("total_cost_usd")
        cost_str = f", ${cost:.2f}" if cost is not None else ""
        print(f"  [grouping_audit] agent finished: {turns} turn(s){cost_str}", flush=True)


def _stream_stdout(process: subprocess.Popen) -> "queue.Queue[str | None]":
    """
    Drain process.stdout on a background thread into a queue, so the
    caller can apply a wall-clock timeout with queue.get(timeout=...)
    even while the process is silently hung (a plain readline loop on
    the main thread would just block forever with no output). Also lets
    us start draining stdout before writing the prompt to stdin, so a
    long prompt can't deadlock against a full stdout pipe buffer.
    """
    q: "queue.Queue[str | None]" = queue.Queue()

    def _reader():
        for line in process.stdout:
            q.put(line)
        q.put(None)  # sentinel: stdout closed

    threading.Thread(target=_reader, daemon=True).start()
    return q


def _invoke_agent(prompt: str) -> bool:
    """
    Run the configured agent CLI with `prompt` on stdin, streaming its
    progress to the console live and logging the raw transcript to
    LOG_FILE for after-the-fact review. Returns whether it actually ran
    to completion - a missing CLI or a timeout doesn't raise, since a
    failed audit is a missed quality-improvement opportunity, not a
    reason to fail the sync that triggered it.
    """
    # subprocess with shell=False resolves a bare command through
    # CreateProcess on Windows, which only auto-appends .exe - never the
    # .cmd shim npm installs CLIs as (unlike a real shell, which does
    # PATHEXT resolution itself). shutil.which() does that resolution for
    # us, so an npm-installed `claude` (found on PATH as claude.cmd) still
    # launches instead of raising FileNotFoundError.
    resolved = shutil.which(AGENT_CLI_COMMAND[0])
    command = [resolved, *AGENT_CLI_COMMAND[1:]] if resolved else AGENT_CLI_COMMAND

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=_ROOT,
        )
    except FileNotFoundError:
        print(f"  [grouping_audit] Agent CLI {AGENT_CLI_COMMAND[0]!r} not found on PATH - "
              f"skipping this audit. Set GROUPING_AGENT_CLI if it's installed under a "
              f"different name.")
        return False

    output_queue = _stream_stdout(process)
    process.stdin.write(prompt)
    process.stdin.close()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + AGENT_TIMEOUT_SECONDS
    timed_out = False
    with LOG_FILE.open("w", encoding="utf-8") as log:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = output_queue.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if line is None:
                break
            log.write(line)
            _print_progress_event(line)

    if timed_out:
        process.kill()
        process.wait()
        print(f"  [grouping_audit] Agent CLI timed out after {AGENT_TIMEOUT_SECONDS}s - "
              f"skipping this audit. Raise GROUPING_AGENT_TIMEOUT if it needs more time.")
        return False

    returncode = process.wait()
    if returncode != 0:
        print(f"  [grouping_audit] Agent CLI exited {returncode} - see {LOG_FILE} for the full transcript.")
        return False

    print("  [grouping_audit] Audit complete.")
    return True


def run_manual_audit() -> None:
    """
    Force an audit at the operator's request (`python run.py audit`),
    outside of any sync and regardless of whether audit_after_sync's
    three triggers would fire on their own. Reuses the periodic-backstop
    prompt (the whole-registry duplicate scan) since that trigger's scope
    isn't tied to what a particular sync changed, unlike new_channels/
    volume_spike - it's the right one for "just audit everything now".
    """
    print("  [grouping_audit] Triggering audit: manual run")
    prompt = _build_prompt("periodic", {})
    ran = _invoke_agent(prompt)
    if ran:
        _save_state({"syncs_since_audit": 0, "last_audit_date": date.today().isoformat(),
                     "last_audit_reason": "manual"})


def audit_after_sync(before: dict) -> None:
    """
    Call once, after discovery.sync_channels and catalog.sync_videos
    both finish (order relative to snapshot.take_snapshot doesn't
    matter - see the module docstring on why grouping and view-count
    collection are independent). Decides whether any trigger fired and,
    if so, hands the registry to the configured agent CLI.
    """
    reason, details = _decide_trigger(before)
    state = _load_state()

    if reason is None:
        state["syncs_since_audit"] = state.get("syncs_since_audit", 0) + 1
        _save_state(state)
        return

    label = {"new_channels": "new channel(s) discovered",
              "volume_spike": "large new-upload batch on an existing channel",
              "periodic": f"periodic ({AUDIT_EVERY_N_SYNCS}-sync) backstop"}[reason]
    print(f"  [grouping_audit] Triggering audit: {label}")

    prompt = _build_prompt(reason, details)
    ran = _invoke_agent(prompt)

    if ran:
        _save_state({"syncs_since_audit": 0, "last_audit_date": date.today().isoformat(),
                     "last_audit_reason": reason})
    else:
        # Don't reset the counter on a failed launch (CLI missing,
        # timeout) - the periodic trigger should keep asking every run
        # until it actually succeeds, rather than going quiet for
        # AUDIT_EVERY_N_SYNCS more syncs over what's likely a one-time
        # environment problem.
        _save_state(state)
