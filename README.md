# Song Ranking Video Automation Engine

An automated pipeline designed to fetch streaming/view metrics, sort track rankings, manage a local video asset library, and programmatically render compilation videos.

## Architecture Pipeline

1. **Data Ingestion** — Scrapes target metadata and real-time view counts via `yt-dlp`.
2. **Asset Management** — Downloads and segments precise 15-second high-definition hook clips.
3. **Video Synthesis Engine** — Utilizes `FFmpeg` to overlay graphical layouts, burn dynamic text, and concatenate compilation sequences.

## Project Structure

```
SongRanking/
├── data/               # Song databases (CSV or SQLite)
├── assets/
│   ├── clips/          # Local library of 15-second music video slices
│   └── templates/      # Fonts, watermarks, background graphics
└── src/                # Automation code / backend
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
make update            # install all Python dependencies
```

## Commands

```bash
python run.py csv                                   # rank and render all songs in CSV
python run.py csv --limit 10                        # top 10 from CSV only

python run.py search                                # search YouTube for "kpop songs"
python run.py search --q "blackpink songs"          # custom query, top 20
python run.py search --q "blackpink songs" --limit 10  # custom query, top 10

python run.py csv --download-workers 8 --encode-workers 4  # tune concurrency, see below
```

> **Note:** Video clips and databases are excluded from version control via `.gitignore` to avoid hitting GitHub file size limits.
>
> `run.py` auto-checks PyPI for a newer `yt-dlp` release (at most once/day) before every `csv`/`search` run and upgrades it automatically if one exists, since a stale `yt-dlp` is the most common cause of silent download failures. The other dependencies are stable enough not to need this — rerun `make update` if you need to refresh them.

## Tuning Worker Pools

`src/pipeline.py` downloads and encodes clips through two independent thread pools: `--download-workers` (default 6) and `--encode-workers` (default 3). They're deliberately separate because they're bound by different resources:

- **Download workers** are limited by network bandwidth *and* by YouTube's own per-IP rate limiting — past a certain concurrency, adding more workers doesn't increase throughput and starts triggering throttled/`403 Forbidden` responses instead.
- **Encode workers** are limited by hardware: GPU encode session capacity (NVENC/QSV/AMF) or CPU core count for the `libx264` fallback.

If downloads can't keep up, extra encode workers just sit idle waiting for clips — there's nothing for them to do. This is the common case on slow connections, and it's why the two pools are sized independently rather than sharing one worker count.

### Rule of thumb by connection speed

Assuming ~15s clips at 1080p, default frame rate (roughly 5-10 MB per clip):

| Connection speed         | `--download-workers` | `--encode-workers` (GPU) | `--encode-workers` (CPU only) | Why |
|---------------------------|:---:|:---:|:---:|---|
| < 25 Mbps                 | 2-3 | 2   | 1-2 | Network-bound — extra encode workers would mostly idle. |
| 25-100 Mbps                | 4-6 (default) | 3 (default) | 2-3 | Balanced; the defaults are a reasonable starting point. |
| 100-300 Mbps               | 6-8 | 3-4 | 3-4 | Bandwidth stops being the limit; YouTube-side rate limiting becomes the real ceiling, so pushing much past 8 rarely helps. |
| 300+ Mbps / very fast link | 8-10 | 4-6 | ~physical cores / 2 | Encode hardware is now the bottleneck — a bigger download pool won't speed things up further. |

For a 200 Mbps connection specifically, start with `python run.py csv --download-workers 6 --encode-workers 4` (GPU) — bandwidth has stopped being the constraint by that point, so there's little upside to a larger download pool, while the encode pool can be sized to hardware capacity since downloads will comfortably keep it fed.

### Estimating manually

- Per-clip download time ≈ `clip_size_MB × 8 / per_connection_mbps` + ~3-5s fixed overhead (connection setup, format negotiation) — on fast connections this fixed cost matters more than raw bandwidth.
- Per-clip encode time ≈ 2-5s on GPU, 10-30s on CPU (`libx264 -preset fast`).
- Encode workers beyond `download_workers × (download_time_per_clip / encode_time_per_clip)` won't have enough incoming clips to stay busy — that's the point past which more `--encode-workers` stops helping.
