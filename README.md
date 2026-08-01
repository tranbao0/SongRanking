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

## Architecture

### 1. Discovery & data layer

Tracks genre-tagged artists and their view-count history. Everything it produces lives in `data/registry.db` (SQLite).

Channel discovery, per genre:

- **Wikidata** - SPARQL query for artists tagged with the genre and a linked YouTube channel (`P2397`). Includes solo artists, not just groups/bands. Source of truth on conflicts.
- **kworb** - each genre's representative country chart on [kworb.net](https://kworb.net/youtube/insights/), resolved to channel IDs via `yt-dlp`. Unioned on top of Wikidata's results.
- **Manual yaml** - `data/channels/<genre>_manual.yaml` (additions) and `data/channels/<genre>_exclude.yaml` (exclusions).

`catalog.py`:
- Walks each channel's uploads via the YouTube Data API.
- Filters to official MVs (`mv_filter.py`: duration window, blocklist for compilations/playlists/etc).
- Groups uploads that are the same underlying song (`song_grouping.py`) so their views aggregate into one chart entry. Additive: a video's `song_id` is set once and never re-derived, so a sync only classifies that channel's new uploads, not its full history. Three tiers: exact normalized-title match against existing videos, local clustering among new uploads, then a Gemini pass for the remainder (also tags titles by Wikidata-confirmed artist on large multi-artist channels to keep different artists' same-titled songs from merging).

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
kworb     ─┼─> discovery.py ─> channels table ─┐
manual    ─┘                                    │
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

- `sync` only updates `data/registry.db` - no download/render.
- `chart` only reads what `sync` last recorded - no fresh YouTube fetch.
- A channel's first `sync` walks its full upload history; later syncs only check for new uploads (see [API spend safeguards](#api-spend-safeguards)).

Shared flags: `--limit` (cap song count), `--download-workers` / `--encode-workers` (see [Tuning worker pools](#tuning-worker-pools)), `--no-filter` (search mode only, skips MV/duration filtering).

## Guide: adding a new genre

1. **Find the genre's Wikidata QID.** [wikidata.org](https://www.wikidata.org) (e.g. "K-pop" is `Q213665`). Add it to `GENRE_QIDS` in `src/registry/providers/wikidata.py`.
2. **Pick a representative kworb country chart.** [kworb.net/youtube/insights/](https://kworb.net/youtube/insights/). Add the two-letter code to `GENRE_COUNTRIES` in `src/registry/providers/kworb.py`.
3. **Create manual curation files:** `data/channels/<genre>_manual.yaml` and `data/channels/<genre>_exclude.yaml` (copy structure from existing `kpop_*`/`jpop_*` files).
4. **Run discovery:** `python run.py sync --genre <genre>`.
5. **Review results** - kworb-sourced channels are most likely to need pruning (see [manual curation](#guide-manual-channel-curation)).
6. **Add chart definitions** in `data/charts.yaml`.

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

Precedence: manual additions > automated sources; exclusions applied last.

## API spend safeguards

`src/shared/api_budget.py` tracks daily usage for the YouTube Data API and Gemini API in `data/.api_usage.json` (resets at midnight, git-ignored). `sync` and AI-assisted steps (title cleanup, song grouping) stop before exceeding budget instead of erroring mid-run.

Defaults: YouTube 10,000 units/day, Gemini 1,500 requests/day. Override in `.env`:

```bash
YOUTUBE_DAILY_QUOTA=10000
GEMINI_DAILY_LIMIT=1500
```

A budget-stopped `sync` resumes from the least-recently-synced channel on the next run - no completed work is redone.

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

> **Note:** Video clips and databases are excluded from version control via `.gitignore` (GitHub file size limits).
>
> `run.py` checks PyPI for a newer `yt-dlp` release (at most once/day) before every `csv`/`search`/`chart` run and upgrades automatically if found.
> Other dependencies: `pip install -r requirements.txt --upgrade`.
