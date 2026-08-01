# Song Ranking Video Automation Engine

An automated pipeline for tracking music video popularity across genres and rendering countdown-style compilation videos from the results.
The system has three components that build on each other: a discovery and data layer that tracks genre-tagged artists and their view-count history over time, a chart engine that turns that history into named rankings, and a render pipeline that turns a ranking into a finished video.

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

This is the part of the system that answers "who counts as an artist in this genre, and how popular are their songs over time."
It is entirely local: everything it produces lives in `data/registry.db` (SQLite), and nothing downstream re-derives it from scratch on every run.

Channel discovery cross-references two sources per genre, plus manual overrides:

- **Wikidata** - a SPARQL query finds every artist tagged with the target genre that has a linked YouTube channel.
This is the authoritative source: an artist Wikidata confirms is kept regardless of whether kworb also finds them.
It is not restricted to bands/groups, so solo artists are included too.
- **kworb** - each genre's representative country chart on [kworb.net](https://kworb.net/youtube/insights/) is used as a popularity-based seed, resolved to channels via `yt-dlp` (no API quota cost).
This catches artists Wikidata does not have yet, but it is not genre-aware, so it is unioned on top of Wikidata's results rather than trusted on its own.
- **Manual yaml files** - `data/channels/<genre>_manual.yaml` and `data/channels/<genre>_exclude.yaml` patch whatever the two automated sources get wrong, in either direction.

Once channels are known, `catalog.py` walks each channel's uploads via the YouTube Data API, filters out non-official-MV content (compilations, wrong duration, etc.), and clusters videos that are really the same song (an official MV plus its dance-practice or lyric-video counterpart, for example) so their view counts aggregate into one chart entry instead of splitting across rows.
`snapshot.py` then records each tracked video's current view count once per run, building up real day-by-day history rather than approximating it.

Run this layer with `python run.py sync`. See [Commands](#commands) below for the full picture, including the cost-saving behavior that makes routine daily syncs cheap.

### 2. Chart engine

A chart is just a declarative combination of genre, ranking metric, and time window, defined in `data/charts.yaml` and computed by `charts.py` purely from the local database - no network calls at all.
Three metrics exist today:

| Metric | Meaning |
|---|---|
| `cumulative` | All-time view count, highest first. |
| `gained` | Views gained over the last `window_days` days, highest first. |
| `newest` | Most recently published, regardless of views. |

Adding a new chart is a config change, not a code change - see [the customization guide](#guide-adding-or-customizing-a-chart).

### 3. Render pipeline

This is the original part of the system: given a ranked list of songs, download a clip of each, overlay the rank/title/artist/badge graphics, and concatenate everything into `final_compilation.mp4`.
It does not care where the ranking came from - it consumes the same shaped data whether it was produced by the chart engine, a live YouTube search, or a hand-maintained CSV.
Each ranking source keeps its own run-over-run history (so "new entry," "re-entry," and rank-change badges work correctly), stored as a CSV per source: `data/songs.csv` for the legacy manual/search workflow, and `data/charts/<chart_name>.csv` per named chart.

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
make update             # install all Python dependencies
```

`YOUTUBE_API_KEY` is required for `sync`/`chart` (and gives much better `csv`/`search` results too).
`GEMINI_API_KEY` is optional; it powers AI title cleanup and same-song grouping, and both features degrade gracefully (skipped, not crashed) if it is absent.

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

`sync` never downloads or renders video - it only updates `data/registry.db`.
`chart` never re-fetches view counts from YouTube for ranking purposes - it reads whatever `sync` last recorded, then renders.
In other words: run `sync` on whatever cadence you want fresh data (daily is the intended cadence), and run `chart` whenever you actually want a video, independent of that.

A first-time `sync` has to walk every channel's full upload history and is comparatively expensive; every `sync` after that is cheap; because it only re-checks channels for new uploads instead of re-walking everything (see [API spend safeguards](#api-spend-safeguards)).

Shared flags: `--limit` (cap song count), `--download-workers` / `--encode-workers` (see [Tuning worker pools](#tuning-worker-pools)), `--no-filter` (search mode only, skips MV/duration filtering).

## Guide: adding a new genre

1. **Find the genre's Wikidata QID.** Search [wikidata.org](https://www.wikidata.org) for the genre (e.g. "K-pop" is `Q213665`) and add it to `GENRE_QIDS` in `src/providers/wikidata.py`.
2. **Pick a representative kworb country chart.** Check [kworb.net/youtube/insights/](https://kworb.net/youtube/insights/) for a country whose chart is a reasonable popularity proxy for the genre, and add its two-letter code to `GENRE_COUNTRIES` in `src/providers/kworb.py`.
This is a seed, not a strict rule - it does not need to be a perfect match.
3. **Create empty manual curation files:** `data/channels/<genre>_manual.yaml` and `data/channels/<genre>_exclude.yaml` (copy the structure/comments from the `kpop_*`/`jpop_*` versions already in that folder).
4. **Run discovery for it:** `python run.py sync --genre <genre>`.
5. **Review the results.** kworb-sourced channels are the ones most likely to need pruning (see [manual curation](#guide-manual-channel-curation)) - a country chart mixes in artists who do not belong to the new genre.
6. **Add chart definitions for it** in `data/charts.yaml` - see the next section.

No other code changes are required. Everything downstream (catalog sync, view snapshots, chart computation, rendering) is already genre-generic.

## Guide: adding or customizing a chart

Charts are pure configuration in `data/charts.yaml`. Each entry:

```yaml
- name: kpop_weekly_gainers   # used as: python run.py chart --name kpop_weekly_gainers
  genre: kpop                 # must match a genre channels are tracked under
  metric: gained               # cumulative | gained | newest
  window_days: 7                # only used by "gained"; use null otherwise
  limit: 50                     # how many songs to include
```

To add a chart, add an entry - no code changes needed for any combination of an existing genre with an existing metric.
A few examples:

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

One real constraint: a `gained` chart's `window_days` is only as accurate as how long `sync` has actually been running daily - there is no approximation from publish date, only real recorded history.
A brand-new 90-day chart will not produce meaningful results until `sync` has been run daily for 90 days.

Adding a genuinely new **metric** (something that is not "sum of views," "delta of views," or "most recent") requires a small code change in `charts.py`'s `compute_chart()` - everything else (loading definitions, grouping by song, rendering) is shared and does not need to change.

## Guide: manual channel curation

`data/channels/<genre>_manual.yaml` and `data/channels/<genre>_exclude.yaml` are hand-maintained and safe to edit directly; `sync` re-reads them every time.

**Manual additions** (patches gaps neither Wikidata nor kworb caught):

```yaml
- channel_id: UCxxxxxxxxxxxxxxxxxxxxxx
  display_name: Artist Name
```

**Exclusions** (drops a channel regardless of which source found it - most useful for kworb noise, since a country's chart can include artists who do not actually belong to the genre):

```yaml
- channel_id: UCxxxxxxxxxxxxxxxxxxxxxx
```

Manual entries always win over both automated sources; exclusions are applied last, after everything else is merged.

## API spend safeguards

`src/api_budget.py` tracks daily usage for both the YouTube Data API and the Gemini API in `data/.api_usage.json` (resets automatically at midnight, git-ignored).
Both `sync` and any AI-assisted step (title cleanup, same-song grouping) stop cleanly before actually exceeding either budget, rather than failing with a raw API error mid-run.

Defaults match YouTube's standard free-tier daily quota (10,000 units) and a conservative placeholder for Gemini.
Override either in `.env` if your actual tier differs:

```bash
YOUTUBE_DAILY_QUOTA=10000
GEMINI_DAILY_LIMIT=1500
```

If a `sync` run gets stopped by the budget guard partway through, nothing is lost - already-completed channels are committed immediately, and the next `sync` run picks up with the least-recently-synced channels first instead of redoing finished work.

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

`src/pipeline.py` downloads and encodes clips through two independent thread pools: `--download-workers` (default 6) and `--encode-workers` (default 3).
They are deliberately separate because they are bound by different resources:

- **Download workers** are limited by network bandwidth *and* by YouTube's own per-IP rate limiting - past a certain concurrency, adding more workers does not increase throughput and starts triggering throttled/`403 Forbidden` responses instead.
- **Encode workers** are limited by hardware: GPU encode session capacity (NVENC/QSV/AMF) or CPU core count for the `libx264` fallback.

If downloads cannot keep up, extra encode workers just sit idle waiting for clips - there is nothing for them to do.
This is the common case on slow connections, and it is why the two pools are sized independently rather than sharing one worker count.

### Rule of thumb by connection speed

Assuming ~15s clips at 1080p, default frame rate (roughly 5-10 MB per clip):

| Connection speed | `--download-workers` | `--encode-workers` (GPU) | `--encode-workers` (CPU only) | Why |
|---|:---:|:---:|:---:|---|
| < 25 Mbps | 2-3 | 2 | 1-2 | Network-bound - extra encode workers would mostly idle. |
| 25-100 Mbps | 4-6 (default) | 3 (default) | 2-3 | Balanced; the defaults are a reasonable starting point. |
| 100-300 Mbps | 6-8 | 3-4 | 3-4 | Bandwidth stops being the limit; YouTube-side rate limiting becomes the real ceiling, so pushing much past 8 rarely helps. |
| 300+ Mbps / very fast link | 8-10 | 4-6 | ~physical cores / 2 | Encode hardware is now the bottleneck - a bigger download pool will not speed things up further. |

For a 200 Mbps connection specifically, start with `python run.py csv --download-workers 6 --encode-workers 4` (GPU) - bandwidth has stopped being the constraint by that point, so there is little upside to a larger download pool, while the encode pool can be sized to hardware capacity since downloads will comfortably keep it fed.

### Estimating manually

- Per-clip download time ≈ `clip_size_MB × 8 / per_connection_mbps` + ~3-5s fixed overhead (connection setup, format negotiation) - on fast connections this fixed cost matters more than raw bandwidth.
- Per-clip encode time ≈ 2-5s on GPU, 10-30s on CPU (`libx264 -preset fast`).
- Encode workers beyond `download_workers × (download_time_per_clip / encode_time_per_clip)` will not have enough incoming clips to stay busy - that is the point past which more `--encode-workers` stops helping.

> **Note:** Video clips and databases are excluded from version control via `.gitignore` to avoid hitting GitHub file size limits.
>
> `run.py` auto-checks PyPI for a newer `yt-dlp` release (at most once/day) before every `csv`/`search`/`chart` run and upgrades it automatically if one exists, since a stale `yt-dlp` is the most common cause of silent download failures.
> The other dependencies are stable enough not to need this - rerun `make update` if you need to refresh them.
