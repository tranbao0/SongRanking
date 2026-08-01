# Song Ranking Video Automation Engine

Pipeline for tracking music video popularity across genres and rendering countdown-style compilation videos from the results.
Three components: a discovery/data layer, a chart engine, and a render pipeline.

## Table of contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Commands](#commands)
- [Guide: adding a new genre](#guide-adding-a-new-genre)
- [Guide: adding or customizing a chart](#guide-adding-or-customizing-a-chart)
- [Guide: manual channel curation](#guide-manual-channel-curation)
- [API spend safeguards](#api-spend-safeguards)
- [Data files](#data-files)
- [Tuning worker pools](#tuning-worker-pools)
- [Tests](#tests)

## Architecture

### 1. Discovery & data layer

Tracks genre-tagged artists and their view-count history. Everything it produces lives in `data/registry.db` (SQLite).

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

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/SongRanking.git
cd SongRanking
cp .env.example .env   # add your GEMINI_API_KEY and YOUTUBE_API_KEY
pip install -r requirements.txt   # install all Python dependencies
```

`YOUTUBE_API_KEY`: required for `sync`/`chart`; also improves `csv`/`search` results.
`GEMINI_API_KEY`: optional, used for AI title cleanup and song grouping; both fall back to non-AI behavior if unset.

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
```

Grouping is additive: a video's `song_id` is set once and never re-derived, which is what keeps a daily `sync` cheap.
The consequence is that a registry grouped under settings you have since changed - or grouped without the AI tier because a budget ran out mid-run - cannot be improved by syncing again, because a sync only looks at videos it has never seen.
`decouple` is the escape hatch: it clears the assignments so grouping can be made afresh.

It never deletes videos. Only the grouping goes.

**Read the confirmation before answering.** The cost is asymmetric:

- A song holding one video loses nothing - re-deriving it is a local normalisation pass.
- A song holding several videos is a real decision, and any made by the AI tier cost API calls to make again.

The prompt reports both counts, and requires you to type `decouple` rather than accept a `y`.
A timestamped backup is written before anything changes, so an accidental run costs no quota to undo - restore the `.pre-decouple.db` file.

- `sync` only updates `data/registry.db` - no download/render.
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
| `data/registry.db` | SQLite: channels, videos, songs (same-song groupings), view_snapshots. Git-ignored. |
| `data/channels/<genre>_manual.yaml` | Hand-curated channel additions per genre. Tracked in git. |
| `data/channels/<genre>_exclude.yaml` | Hand-curated channel exclusions per genre. Tracked in git. |
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
