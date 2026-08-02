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
back-and-forth possible) with direct write access to data/registry.db -
it applies its own fixes rather than proposing a diff for a human,
since this is a lone unattended pass with no concurrent writer to race.
"""

import json
import os
import subprocess
from datetime import date
from pathlib import Path

from registry import db

_ROOT = Path(__file__).parent.parent.parent
STATE_FILE = _ROOT / "data" / ".grouping_audit_state.json"
METHODOLOGY_DOC = "docs/manual-grouping-prompt.md"
PROGRESS_FILE = "data/.manual-grouping-progress.json"

# The agent CLI to invoke, as argv tokens - not hardcoded to one vendor.
# Point this at whatever subscription-based coding-agent CLI is
# installed and authenticated; the only requirement is that it accepts a
# one-shot prompt on stdin and can read/write files in the given cwd.
# Default matches Claude Code's headless/print mode.
AGENT_CLI_COMMAND = os.environ.get("GROUPING_AGENT_CLI", "claude -p").split()

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
at data/registry.db, in the SongRanking project rooted at the current working directory.
This is a one-shot, non-interactive run - there is no human to ask a clarifying question,
so make the conservative call (per the doc's "when unsure, don't merge" rule) rather than
waiting on anything.

Read {METHODOLOGY_DOC} in full before doing anything else. It has the complete decided
rules, the DO-NOT-merge list, the Making/Behind excision rule and its exact SQL patterns,
and a large "Precedents from prior runs" section with dozens of worked examples - apply
those rather than re-deriving them.

Trigger and scope for this run:
{scope}

You have direct read/write access to data/registry.db - apply merges and excisions
directly as you find them (see the doc's "How to apply a merge" / "How to excise"
sections), you don't need to produce a diff for a human to review since this is a single
unattended pass with nothing else writing to the database concurrently.

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


def _invoke_agent(prompt: str) -> bool:
    """
    Run the configured agent CLI with `prompt` on stdin. Returns whether
    it actually ran to completion - a missing CLI or a timeout doesn't
    raise, since a failed audit is a missed quality-improvement
    opportunity, not a reason to fail the sync that triggered it.
    """
    try:
        result = subprocess.run(
            AGENT_CLI_COMMAND,
            input=prompt,
            text=True,
            cwd=_ROOT,
            timeout=AGENT_TIMEOUT_SECONDS,
            capture_output=True,
        )
    except FileNotFoundError:
        print(f"  [grouping_audit] Agent CLI {AGENT_CLI_COMMAND[0]!r} not found on PATH - "
              f"skipping this audit. Set GROUPING_AGENT_CLI if it's installed under a "
              f"different name.")
        return False
    except subprocess.TimeoutExpired:
        print(f"  [grouping_audit] Agent CLI timed out after {AGENT_TIMEOUT_SECONDS}s - "
              f"skipping this audit. Raise GROUPING_AGENT_TIMEOUT if it needs more time.")
        return False

    if result.returncode != 0:
        print(f"  [grouping_audit] Agent CLI exited {result.returncode}:")
        print(result.stderr.strip()[-2000:])
        return False

    print("  [grouping_audit] Audit complete.")
    return True


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
