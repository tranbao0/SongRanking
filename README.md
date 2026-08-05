# Song Ranking Video Automation Engine

Pipeline for tracking music video popularity across genres and rendering countdown-style compilation videos from the results.
Three components: a discovery/data layer, a chart engine, and a render pipeline.

## Table of contents

- [Before you start](#before-you-start)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Hosted registry database (Turso)](#hosted-registry-database-turso)
- [Commands](#commands)
- [Guide: adding a new genre](#guide-adding-a-new-genre)
- [Guide: adding or customizing a chart](#guide-adding-or-customizing-a-chart)
- [Guide: manual channel curation](#guide-manual-channel-curation)
- [Guide: pinning individual videos](#guide-pinning-individual-videos)
- [Guide: keeping song grouping accurate](#guide-keeping-song-grouping-accurate)
- [API spend safeguards](#api-spend-safeguards)
- [yt-dlp bot-check fallback](#yt-dlp-bot-check-fallback)
- [Automated grouping audits](#automated-grouping-audits)
- [Data files](#data-files)
- [Tuning worker pools](#tuning-worker-pools)
- [Tests](#tests)

## Before you start

This is a working pipeline, not a turnkey product. Cloning it and running `sync` produces *something*, but not something good, until a few things are done by hand - none of these are bugs waiting to be fixed later, they're inherent to what this project is doing:

- **Channel coverage requires manual curation, not just Wikidata.** Wikidata only links artists to their *own* channel; label, distributor, and broadcaster channels - which carry a large share of real uploads - have no entry there at all and have to be added to `data/channels/<genre>_manual.yaml` by hand. Skip this and your registry is missing most of what actually charts. See [manual channel curation](#guide-manual-channel-curation).
- **The automated grouping tiers alone are not good enough for a fresh channel.** They merge the easy cases; a channel's first sync still leaves real duplicates (the same song as separate chart entries) that need an actual read-through to catch, especially cross-channel duplicates and judgment-heavy presentation-format calls no regex or one-shot AI chunk can resolve. Budget for either running the manual review process yourself (see [keeping song grouping accurate](#guide-keeping-song-grouping-accurate)) or setting up the [automated audit hook](#automated-grouping-audits) - which itself needs a coding-agent CLI installed and authenticated separately, it is not covered by your `.env` API keys.
- **API keys have real, separate limits to manage.** `YOUTUBE_API_KEY` is required for `sync`/`chart` and its quota is fixed by Google regardless of your billing tier. `GEMINI_API_KEY` is optional, but a large first sync is meaningfully more accurate with it than without. See [API spend safeguards](#api-spend-safeguards).
- **The render pipeline's clip downloads can hit YouTube's bot-check wall outright** ("Sign in to confirm you're not a bot"), independent of anything above - this is IP/session-level, not something pacing requests fixes. The pipeline retries through an alternate client and finally exported browser cookies automatically, but that last tier needs a one-time `cookies.txt` export - see [yt-dlp bot-check fallback](#yt-dlp-bot-check-fallback).
- **You have to define your own charts.** `data/charts.yaml` ships empty - see [adding or customizing a chart](#guide-adding-or-customizing-a-chart).
- **Render workers need tuning to your own hardware/connection**, or the render step bottlenecks somewhere you didn't expect - see [tuning worker pools](#tuning-worker-pools).

If you only do the [Setup](#setup) steps below and nothing else, `sync` will run without erroring - it just won't produce a registry worth charting from.

## Architecture

### 1. Discovery & data layer

Tracks genre-tagged artists and their view-count history. Everything it produces lives in a hosted Turso (libSQL) database - see [Hosted registry database (Turso)](#hosted-registry-database-turso) - shared across every machine that runs `sync`/`chart`.

Channel discovery, per genre:

- **Wikidata** - SPARQL query for artists tagged with the genre and a linked YouTube channel (`P2397`). Includes solo artists, not just groups/bands. The only automated source, and authoritative.
- **Manual yaml** - `data/channels/<genre>_manual.yaml` (additions) and `data/channels/<genre>_exclude.yaml` (exclusions).
  Wikidata links an artist to their *own* channel, so label, distributor and broadcaster channels have no entry there however much of the genre they carry. Those are curated by hand.

> A popularity-seeded provider (per-country YouTube charts) was tried and removed.
> A country chart ranks what charts *in* a market, not what belongs to a genre, so it kept introducing acts from other genres entirely - and because their uploads are labelled impeccably by their own labels, no title-level filter could tell them apart from the real thing.
> Genre membership has to be decided per channel, which is what the two yaml files are for.

`catalog.py`:
- Walks each channel's uploads via the YouTube Data API.
- Filters to official MVs (`mv_filter.py`: duration window, blocklist for compilations/playlists/etc).
- Groups uploads that are the same underlying song (`song_grouping.py`) so their views aggregate into one chart entry.
  Additive: a video's `song_id` is set once and never re-derived, so a sync only classifies that channel's new uploads, not its full history.
  Three tiers, cheapest first: exact normalized-title match against existing videos, local clustering among new uploads, then a Gemini pass for whatever is left.
  The first two tiers are free and deterministic, and normalization strips upload-type markers whether or not they are bracketed (`'Dynamite' Official MV` and `'Dynamite' Dance Practice` resolve to the same key), so most same-song pairs never reach the AI at all.
  On large multi-artist channels the AI tier also tags each title with its Wikidata-confirmed artist, to keep different artists' same-titled songs from merging.
  A re-arranged version counts as a different song: remixes, acoustic, instrumental and sped-up versions each chart separately from the original.

`snapshot.py` records each tracked video's current view count once per run (`view_snapshots` table).

Run with `python run.py sync`.

### 2. Chart engine

A chart = genre + metric + time window, defined in `data/charts.yaml`, computed by `charts.py` from the local database only (no network calls).

| Metric | Meaning |
|---|---|
| `cumulative` | All-time view count, highest first. |
| `gained` | Views gained over the last `window_days` days, highest first. |
| `newest` | Most recently published, regardless of views. |

A song group's `url`/`title` for rendering come from its highest-individual-views member. Adding a chart is a config change - see [the customization guide](#guide-adding-or-customizing-a-chart).

### 3. Render pipeline

Given a ranked list of songs: download a clip of each, overlay rank/title/artist/badge graphics, concatenate into `final_compilation.mp4`.
Same renderer regardless of ranking source (chart engine, live search, or manual CSV). Each source keeps its own run-history CSV for rank-change/new-entry/re-entry badges: `data/songs.csv` (legacy manual/search workflow) or `data/charts/<chart_name>.csv` (per named chart).

```
Wikidata ─┐
manual    ─┼─> discovery.py ─> channels table ─┐
exclude   ─┘                                    │
                                                 v
                                          catalog.py ─> videos + songs tables
                                                 │
                                                 v
                                          snapshot.py ─> view_snapshots table
                                                 │
                                                 v
                                          charts.py ─> ranked song list (per data/charts.yaml)
                                                 │
                                                 v
                                   render pipeline ─> final_compilation.mp4
                          (same renderer also used directly by `csv` / `search`)
```

## Prerequisites

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [FFmpeg](https://ffmpeg.org/)
- Python 3.10+
- [Turso CLI](https://docs.turso.tech/cli/installation) - only needed for the one-time database setup below (and wherever `sync`'s [automated grouping audit](#automated-grouping-audits) runs). **On Windows this requires WSL** - the install script is bash-only; run it from a WSL shell.

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/SongRanking.git
cd SongRanking
cp .env.example .env   # add your GEMINI_API_KEY, YOUTUBE_API_KEY, and TURSO_DATABASE_URL/TURSO_AUTH_TOKEN
pip install -r requirements.txt   # install all Python dependencies
```

`YOUTUBE_API_KEY`: required for `sync`/`chart`; also improves `csv`/`search` results.
`GEMINI_API_KEY`: optional, used for AI title cleanup and song grouping; both fall back to non-AI behavior if unset.
`TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`: required for `sync`/`chart`/`decouple`/`regroup` - see below.

## Hosted registry database (Turso)

The registry (`channels`, `videos`, `songs`, `view_snapshots`) lives in a private [Turso](https://turso.tech/) (hosted libSQL) database rather than a local SQLite file.
This exists because the registry used to be `data/registry.db`, a local file inside this repo's OneDrive-synced folder - and OneDrive gives no real multi-writer safety for a single binary file: a delete on one machine silently propagates to every synced machine, with no real history beyond whatever survives in a Recycle Bin.
That cost a real data-loss incident.

`sync`/`decouple`/`regroup` don't read/write the hosted database statement-by-statement, though - each opens with exactly one network round trip (pull the current hosted state into a local working copy, `data/.registry_working.db`) and closes with exactly one more (push that copy's final state back), same as this project's own backup-and-import discipline. Everything in between - the actual API-driven catalog work, `grouping_audit.py`'s headless agent - runs against that local file at local-disk speed, with zero network calls to Turso in the middle. See `src/registry/db.py`'s module docstring for the full reasoning; a statement-per-round-trip design was tried first and was too slow at this project's scale.

**One-time setup** (once per registry, not per machine):

```bash
turso auth login                          # or: turso auth signup
turso db import path/to/your/registry.db  # seed a new hosted db from an existing local file
                                           # (names the database after the file, e.g. "registry")
turso db show registry --url              # -> TURSO_DATABASE_URL
turso db tokens create registry           # -> TURSO_AUTH_TOKEN
```

Add both to `.env`:

```bash
TURSO_DATABASE_URL=libsql://registry-your-org.turso.io
TURSO_AUTH_TOKEN=your_token_here
```

**Every other machine** just needs the same two `.env` values - no shared local file, no per-machine setup beyond that; each machine's own `sync` pulls a fresh working copy for itself.

`decouple`'s backup (see [Redoing grouping](#redoing-grouping)) is a plain copy of that local working copy, taken before decouple's own changes land - restoring it means putting it back at `data/.registry_working.db` before `push_from_local()` runs (or importing it into a fresh Turso database directly with `turso db import <backup-file>` if the bad state already got pushed).

`sync`/`decouple`/`regroup` each pull then push automatically, but the two halves are also available on their own:

```bash
python run.py pull    # overwrite the local working copy with Turso's current state
python run.py push    # overwrite Turso with the local working copy's current state
```

`pull` writes a timestamped backup of whatever the local working copy held (`data/registry.<timestamp>.pre-pull.db`) before overwriting it, and keeps only the 3 most recent such backups - restoring one means putting it back at `data/.registry_working.db` before `push` runs.
`push` has no equivalent backup on the Turso side, since Turso itself is the durable copy - export one first with the `turso` CLI (`turso db shell registry .dump`, or see [scripts/turso_shell.sh](scripts/turso_shell.sh)) if you want a point-in-time snapshot of what's currently live before overwriting it.

## Commands

### Legacy manual workflow (unaffected by the genre system below)

```bash
python run.py csv                                   # rank and render all songs in data/songs.csv
python run.py csv --limit 10                        # top 10 from that CSV only

python run.py search                                # search YouTube for "kpop songs"
python run.py search --q "blackpink songs"          # custom query, top 20
python run.py search --q "blackpink songs" --limit 10

python run.py clean                                  # re-run AI title cleanup over data/songs.csv in place
```

### Genre-aware workflow

```bash
python run.py sync                          # refresh channels/videos/view snapshots for every known genre
python run.py sync --genre kpop              # refresh a single genre (repeatable: --genre kpop --genre jpop)

python run.py chart --name kpop_alltime      # compute + render a named chart from data/charts.yaml
python run.py chart --name kpop_alltime --limit 10
```

### Redoing grouping

```bash
python run.py decouple                  # clear song assignments for every genre
python run.py decouple --genre kpop     # one genre only
python run.py decouple --yes            # skip the confirmation (scripts)

python run.py regroup                   # re-derive groupings decouple cleared
python run.py regroup --genre kpop      # one genre only
```

### Manual registry pull/push

`sync`/`decouple`/`regroup` each pull then push automatically around their own work - these expose the two halves on their own, for inspecting or restoring the registry without running a full command:

```bash
python run.py pull                      # overwrite the local working copy with Turso's current state
python run.py push                      # overwrite Turso with the local working copy's current state
```

Grouping is additive: a video's `song_id` is set once and never re-derived, which is what keeps a daily `sync` cheap.
The consequence is that a registry grouped under settings you have since changed - or grouped without the AI tier because a budget ran out mid-run - cannot be improved by syncing again, because a sync only looks at videos it has never seen.
`decouple` is the escape hatch: it clears the assignments so grouping can be made afresh.

It never deletes videos. Only the grouping goes.

**Read the confirmation before answering.** The cost is asymmetric:

- A song holding one video loses nothing - re-deriving it is a local normalisation pass.
- A song holding several videos is a real decision, and any made by the AI tier cost API calls to make again.

The prompt reports both counts, and requires you to type `decouple` rather than accept a `y`.
A timestamped local backup (`data/registry.<timestamp>.pre-decouple.db`) is written before anything changes, so an accidental run costs no quota to undo (see [Hosted registry database (Turso)](#hosted-registry-database-turso) for how to restore it).

`regroup` is the other half: it runs the videos `decouple` left with a NULL `song_id` back through the same grouping tiers, channel by channel.
It makes no YouTube API call - every candidate was already fetched and MV-filtered on a prior sync, so only the grouping is redone, not the catalogue.
It costs nothing beyond whatever the AI tier spends (subject to the same daily Gemini budget as `sync`), and is safe to interrupt and re-run: a channel with nothing left ungrouped is skipped.

- `sync` only updates the hosted registry - no download/render.
- `chart` only reads what `sync` last recorded - no fresh YouTube fetch.
- A channel's first `sync` walks its full upload history; later syncs only check for new uploads (see [API spend safeguards](#api-spend-safeguards)).

Shared flags: `--limit` (cap song count), `--download-workers` / `--encode-workers` (see [Tuning worker pools](#tuning-worker-pools)), `--no-filter` (search mode only, skips MV/duration filtering).

## Guide: adding a new genre

1. **Find the genre's Wikidata QID.** [wikidata.org](https://www.wikidata.org) (e.g. "K-pop" is `Q213665`). Add it to `GENRE_QIDS` in `src/registry/providers/wikidata.py`.
2. **Create manual curation files:** `data/channels/<genre>_manual.yaml` and `data/channels/<genre>_exclude.yaml` (copy structure from existing `kpop_*`/`jpop_*` files).
3. **Run discovery:** `python run.py sync --genre <genre>`.
4. **Review results**, then curate. Wikidata gives you artists; add the genre's label/distributor channels to the manual file yourself, since those carry a large share of the catalogue and Wikidata has no entry for them.
5. **Add chart definitions** in `data/charts.yaml`.

No other code changes required - catalog sync, view snapshots, chart computation, and rendering are genre-generic.

## Guide: adding or customizing a chart

Charts are configuration in `data/charts.yaml`. Each entry:

```yaml
- name: kpop_weekly_gainers   # used as: python run.py chart --name kpop_weekly_gainers
  genre: kpop                 # must match a genre channels are tracked under
  metric: gained               # cumulative | gained | newest
  window_days: 7                # only used by "gained"; use null otherwise
  limit: 50                     # how many songs to include
```

Adding a chart = adding an entry, for any combination of an existing genre + metric.

```yaml
- name: jpop_monthly_gainers
  genre: jpop
  metric: gained
  window_days: 30
  limit: 50

- name: kpop_yearly_gainers
  genre: kpop
  metric: gained
  window_days: 365
  limit: 100
```

`gained`'s `window_days` accuracy is bounded by how long `sync` has been run daily - a new 90-day window needs 90 days of recorded history first.

Adding a new **metric** (not "sum," "delta," or "most recent") requires a code change in `charts.py`'s `compute_chart()`.

## Guide: manual channel curation

`data/channels/<genre>_manual.yaml` / `_exclude.yaml` are hand-edited directly; `sync` re-reads them every run.

**Manual additions:**

```yaml
- channel_id: UCxxxxxxxxxxxxxxxxxxxxxx
  display_name: Artist Name
```

**Exclusions** (drops a channel regardless of source):

```yaml
- channel_id: UCxxxxxxxxxxxxxxxxxxxxxx
```

Precedence: manual additions override Wikidata; exclusions are applied last and win over everything.

Excluding a channel also deletes it and everything catalogued from it - its videos, and any song left with no videos.
That matters because charts read videos joined to channels, so an excluded channel's uploads would otherwise keep charting under the genre they were wrongly tagged with.

Only official artist, label, distributor or broadcaster channels belong in the manual file.
Fan lyric-video channels and re-uploaders carry the right genre but the wrong uploads: their views are separate from the official release, so counting them splits a song's audience across copies instead of measuring it.

## Guide: pinning individual videos

Sometimes exactly one song lives on a channel that's mostly a *different* genre entirely - a movie studio's channel, a late-night talk show's channel - so adding it to `<genre>_manual.yaml` would also adopt everything else that channel has ever posted (trailers, unrelated interviews, other shows' clips). `data/channels/<genre>_manual_videos.yaml` pins the individual video ID(s) instead:

```yaml
- video_id: xxxxxxxxxxx
  channel_id: UCxxxxxxxxxxxxxxxxxxxxxx
  display_name: Free-text label, for readability only
```

`sync` still needs a `channels` row for the video's real channel (videos have a foreign key to it), so one is created automatically with `source: manual_video` - but that channel's upload history is never walked, and its pinned video(s) skip the MV title/duration filter entirely (a pinned video is already a deliberate, one-by-one choice, not something a heuristic should second-guess).

Keep this to the same *kind* of channel `<genre>_manual.yaml` already allows - a label, distributor, or other channel acting as the release's own official source - not a talk show or awards show's performance clip of it. No song in the registry has its broadcaster performances tracked, since that content isn't reachable through any artist's own or label channel; pinning it for one song but not the rest would count that song by a looser standard than everything else on the chart. (If a pinned song's official upload and another pinned upload of the same song end up on two different channels regardless, `sync` does still share grouping context across every `manual_video` channel in a genre, so they merge into one entry rather than charting separately.)

Real example, from `data/channels/kpop_manual_videos.yaml`: the *KPop Demon Hunters* soundtrack sits on Sony Pictures Animation's channel, which otherwise posts unrelated film content and would never be added there as a full manual channel.

## Guide: keeping song grouping accurate

`sync`'s deterministic + AI grouping tiers (see [Architecture](#architecture)) handle the common case cheaply, but they are structurally limited, not just imperfectly tuned:

- The AI tier only ever sees one chunk of one channel at a time, so it cannot catch the same song uploaded to a *different* channel (an artist's own channel and a label/aggregator channel like a broadcaster both posting the same MV).
- Neither tier can apply judgment to genuinely ambiguous cases - is a `(Rock Ver.)` tag a real rearrangement or just a stage cut? Is a bracketed `[Artist TV Behind]` show name actually behind-the-scenes footage, or a real performance video released under a branded show? - the kind of call that only comes from reading the title (and often the native-script text) directly.

`docs/manual-grouping-prompt.md` is the accumulated methodology for doing that reading by hand, built up over repeated audits of this project's own registry: the decided rules (what merges, what doesn't), a large "Precedents from prior runs" section of worked examples, and the exact SQL for applying a fix once you've found one. It's meant to be pasted directly into a fresh coding-agent session (Claude Code or similar) pointed at your `data/registry.db`.

**When you need this:**
- After a channel's *first* sync (its whole catalog is unreviewed).
- After any sync that adds an unusually large batch to one channel (e.g. a full back-catalog dump).
- Periodically, regardless of volume, to catch cross-channel duplicates the per-channel tiers structurally can't see.

That's exactly the trigger set [the automated audit hook](#automated-grouping-audits) below runs on your behalf if you set it up - the doc above is what it reads, and it applies the same rules a human session would. Running it by hand is the fallback if you'd rather not (or can't yet) configure that hook.

## API spend safeguards

`src/shared/api_budget.py` tracks daily usage for the YouTube Data API and Gemini API in `data/.api_usage.json` (resets at midnight, git-ignored).
`sync` and AI-assisted steps (title cleanup, song grouping) stop before exceeding budget instead of erroring mid-run.

Defaults: YouTube 10,000 units/day, Gemini 500 requests/day. Override in `.env`:

```bash
YOUTUBE_DAILY_QUOTA=10000
GEMINI_DAILY_LIMIT=500
```

`YOUTUBE_DAILY_QUOTA` matches YouTube's standard free-tier allocation.
That allocation is fixed - raising it goes through a quota extension request rather than billing - so cloud credit does not lift it and there is rarely a reason to change this value.

`GEMINI_DAILY_LIMIT` is a spend guard, not an estimate of any tier's rate limit.
Gemini bills per token, so the request count isn't the cost directly, but capping requests bounds a runaway loop - which is the failure worth insuring against on a small prepaid balance.
The default is deliberately low because the AI tier is optional: the free grouping tiers do the bulk of the work, so a sync that stops early loses little.
On a first full `sync` this value, not wall-clock speed, is what caps how many channels complete per day - raise it if the bootstrap keeps stopping before you want it to.

### Running without Gemini at all

Leave `GEMINI_API_KEY` unset and the AI steps are skipped entirely, reporting once rather than per call.
Grouping still runs its two free tiers; anything they can't match stays its own song, and nothing is dropped.

On the current registry that costs less than it sounds.
The free tiers already merge the predictable cases, and the residue the AI tier would judge is dominated by pairs that should stay separate anyway - remixes, and different songs by one artist whose titles overlap because the artist's name dominates them.

A budget-stopped `sync` resumes from the least-recently-synced channel on the next run - no completed work is redone.

## yt-dlp bot-check fallback

Every yt-dlp call in the render pipeline (clip download in `encoding.py`, heatmap extraction in `heatmap.py`) can hit YouTube's "Sign in to confirm you're not a bot" wall. Two distinct causes look identical in the error message, so there are two distinct fixes, tried in order automatically:

1. **Rate-triggered** - firing many yt-dlp requests at once is what actually trips this in practice. `src/shared/ytdlp_throttle.py` paces every yt-dlp launch (across every worker thread and call site: metadata fetch, heatmap extraction, clip download) at least `YTDLP_MIN_REQUEST_INTERVAL` seconds apart (default 0.35).
2. **IP/session-flagged** - no amount of pacing gets past this; only an authenticated request does. The pipeline retries with the `android` player client first (fixes a separate, unrelated extraction bucketing issue), then as a last resort with cookies from a real logged-in session.

That last resort needs a one-time export, since reading cookies directly out of a running browser's profile is unreliable on Windows: the profile's cookie DB is locked while the browser is open, and recent Chrome/Edge encrypt cookies with App-Bound Encryption, which yt-dlp cannot decrypt from a separate process ([yt-dlp#10927](https://github.com/yt-dlp/yt-dlp/issues/10927)) even once the lock is out of the way.

**One-time setup:**

1. Install the **"Get cookies.txt LOCALLY"** extension (Chrome Web Store, also installs on Chromium Edge).
2. Log into YouTube in that browser, go to youtube.com, click the extension, export cookies for the site. It writes Netscape format directly (starts with `# Netscape HTTP Cookie File`) - no conversion needed.
3. Save the export as `cookies.txt` in the repo root (git-ignored already).
4. Add to `.env`:
   ```bash
   YTDLP_COOKIES_FILE=cookies.txt
   ```

Never commit that file - it carries a live YouTube session. If `YTDLP_COOKIES_FILE` is unset, the fallback instead tries `--cookies-from-browser` on `YTDLP_COOKIES_BROWSER` (default `edge`), which works only if that browser is closed and its cookies aren't App-Bound-encrypted - the file-based export is the reliable path.

## Automated grouping audits

`src/registry/grouping_audit.py` hands the registry to a subscription-based coding-agent CLI (Claude Code by default) for the judgment-heavy review described in [keeping song grouping accurate](#guide-keeping-song-grouping-accurate), automatically, as part of `sync`.

**This needs its own one-time setup and does nothing out of the box.** It shells out to a CLI (`claude -p` by default) that must already be installed *and authenticated* on the machine running `sync` - there is no API key for this in `.env`, the subprocess inherits whatever login state that CLI already has. The agent reaches the registry through the same local working copy `sync` already pulled (see [Hosted registry database (Turso)](#hosted-registry-database-turso)) - a plain local SQLite file, no turso CLI needed for this routine path. If the agent CLI isn't available, `sync` logs a one-line warning and moves on; the audit is skipped, the sync itself is not affected.

Three independent triggers, checked after every `sync`, any one of which is enough:

| Trigger | Fires when | Scope handed to the agent |
|---|---|---|
| New channel | A channel appeared in `channels` this sync | Just that channel's full catalog |
| Volume spike | One channel's new-upload count this sync ≥ `GROUPING_AUDIT_VOLUME_THRESHOLD` | Just that channel |
| Periodic backstop | `GROUPING_AUDIT_EVERY_N_SYNCS` syncs have passed with neither of the above | A general cross-channel duplicate sweep |

Override in `.env`:

```bash
GROUPING_AGENT_CLI=claude -p          # which CLI to invoke - point elsewhere if you use a different agent
GROUPING_AUDIT_EVERY_N_SYNCS=7        # periodic backstop cadence, in number of syncs
GROUPING_AUDIT_VOLUME_THRESHOLD=50    # new uploads on one channel in one sync that counts as a "spike"
GROUPING_AGENT_TIMEOUT=3600           # seconds to wait before giving up on the agent
```

The agent runs headless (its prompt piped over stdin, one shot, no back-and-forth), reads `docs/manual-grouping-prompt.md` for the full methodology, and is told to apply fixes directly to the local working copy rather than propose a diff - safe here because this is a lone unattended pass with nothing else writing to it at the same time, and its edits become part of what `sync` pushes back to Turso once it finishes. It's also told to fold anything it learns that isn't already in that doc's precedents section back into it, so the next audit (human or agent) inherits the judgment call instead of re-deriving it.

It's safe to run alongside routine syncs, and safe to skip a cycle if the agent CLI isn't available: a missed correction just sits as a minor chart inaccuracy until the next audit catches it. `view_snapshots` keys on `video_id`, not `song_id`, and a chart re-aggregates through `videos.song_id` at render time - so a late grouping fix never loses or corrupts anything already collected.

State (which trigger last fired, how many quiet syncs since) lives in `data/.grouping_audit_state.json`, git-ignored like the other sidecar caches.

### What a run actually spends

**YouTube** costs 1 unit per 50 videos walked, and again 1 unit per 50 durations checked.
A channel's first sync walks its entire upload history, so a 4,000-upload channel costs ~160 units on its own; later syncs cost 1 unit when the video count hasn't changed.
Titles the blocklist already rejects are dropped before their durations are fetched, so no quota is spent resolving videos that cannot qualify.

**Gemini** is billed per token, and a request's instruction preamble, candidate-song list and thinking cost are all paid per call regardless of how many titles it carries.
Grouping therefore sends large chunks (`CHUNK_SIZE` in `src/shared/gemini_client.py`) rather than many small ones - see the measured table in that file.
Failures are retried at two levels, because song grouping is additive: a chunk that fails is never revisited, so its videos would stay permanently ungrouped.
Rate limits and transient server errors are retried with jittered backoff inside `gemini_client`, and a response that arrives but can't be parsed is re-asked once by `song_grouping` (sampling is stochastic, so a re-ask usually succeeds).
Errors that cannot succeed - a bad key, a malformed request - are not retried at all, since every attempt is a billed request.

## Data files

| Path | What it is |
|---|---|
| Hosted Turso database | channels, videos, songs (same-song groupings), view_snapshots - the durable source of truth. See [Hosted registry database (Turso)](#hosted-registry-database-turso). |
| `data/.registry_working.db` | Local working copy `sync`/`decouple`/`regroup`/`pull`/`push` pull from Turso, do their own work against, and push back. Ephemeral - safe to delete. Git-ignored. |
| `data/registry.<timestamp>.pre-decouple.db` | Local backup written before a `decouple`. Git-ignored. |
| `data/registry.<timestamp>.pre-pull.db` | Local backup of the working copy written before a `pull` overwrites it. Only the 3 most recent are kept. Git-ignored. |
| `data/channels/<genre>_manual.yaml` | Hand-curated channel additions per genre. Tracked in git. |
| `data/channels/<genre>_exclude.yaml` | Hand-curated channel exclusions per genre. Tracked in git. |
| `data/channels/<genre>_manual_videos.yaml` | Hand-curated individually pinned videos per genre. Tracked in git. |
| `data/charts.yaml` | Named chart definitions. Tracked in git. |
| `data/songs.csv` | Legacy manual/search workflow's working file and run-history. Tracked in git. |
| `data/charts/<chart_name>.csv` | Per-chart working file and run-history, one per entry in `charts.yaml`. |
| `data/.api_usage.json` | Today's API spend counters. Git-ignored, resets daily. |

## Tuning worker pools

There are three pools in total: two in the render pipeline, exposed as CLI flags, and one in song grouping, set in code.

### Song grouping (Gemini)

`_AI_CHUNK_WORKERS` in `src/registry/song_grouping.py` (default 4) controls how many grouping requests are in flight at once.
A grouping call is ~10s of mostly waiting, and a channel can need several, so overlapping them is the difference between a sync that looks responsive and one that looks hung.

This pool is safe to widen or narrow because **the chunks are independent by construction**.
Every chunk is handed the same snapshot of already-known songs, that snapshot is never updated mid-pass, and no song row is written until all chunks have returned.
Concurrency therefore cannot change which videos end up grouped together - results are also consumed in submission order, so even the assigned `song_id` values match what a sequential run produces.

Raise it if you are on a paid tier with generous rate limits and want a first sync to finish sooner.
Lower it to 1 if you are hitting 429s more often than the retry/backoff absorbs.
Note that widening this does not reduce spend at all - it only hides latency - and on a first sync your daily Gemini budget, not this number, is what limits progress.

Chunk *size* is the setting that affects cost and accuracy; see `CHUNK_SIZE` in `src/shared/gemini_client.py`, which carries the benchmark it was chosen from.

### Download and encode

`src/render/pipeline.py` downloads and encodes clips through two independent thread pools: `--download-workers` (default 6), `--encode-workers` (default 3).

- **Download workers**: bounded by network bandwidth and YouTube's per-IP rate limiting.
- **Encode workers**: bounded by hardware - GPU encode session capacity (NVENC/QSV/AMF) or CPU core count (`libx264` fallback).

### Rule of thumb by connection speed

Assuming ~15s clips at 1080p, default frame rate (roughly 5-10 MB per clip):

| Connection speed | `--download-workers` | `--encode-workers` (GPU) | `--encode-workers` (CPU only) | Bottleneck |
|---|:---:|:---:|:---:|---|
| < 25 Mbps | 2-3 | 2 | 1-2 | Network |
| 25-100 Mbps | 4-6 (default) | 3 (default) | 2-3 | Balanced |
| 100-300 Mbps | 6-8 | 3-4 | 3-4 | YouTube-side rate limiting |
| 300+ Mbps / very fast link | 8-10 | 4-6 | ~physical cores / 2 | Encode hardware |

For a 200 Mbps connection: `python run.py csv --download-workers 6 --encode-workers 4` (GPU).

### Estimating manually

- Per-clip download time ≈ `clip_size_MB × 8 / per_connection_mbps` + ~3-5s fixed overhead (connection setup, format negotiation).
- Per-clip encode time ≈ 2-5s on GPU, 10-30s on CPU (`libx264 -preset fast`).
- Encode workers beyond `download_workers × (download_time_per_clip / encode_time_per_clip)` sit idle.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Standard library `unittest`, no extra dependencies and no test runner to install.

No test reaches the network, spends API quota, or touches `data/registry.db` - every one builds its own in-memory database, and the YouTube and Gemini calls are stubbed.
The suite is therefore safe to run while a `sync` is in progress.

Several tests are differential: they keep a previous implementation as an oracle and assert the current one matches it across randomized inputs.
That is how the SQL rewrite of the view lookup and the artist-matching index are pinned, and any test whose docstring says *"Do not optimise"* is one of those oracles.

> **Note:** Video clips and databases are excluded from version control via `.gitignore` (GitHub file size limits).
>
> `run.py` checks PyPI for a newer `yt-dlp` release (at most once/day) before every `csv`/`search`/`chart` run and upgrades automatically if found.
> Other dependencies: `pip install -r requirements.txt --upgrade`.
