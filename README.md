# K-Pop Chart Video Automation Engine

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
python run.py update                                # update all dependencies

python run.py csv                                   # rank and render all songs in CSV
python run.py csv --limit 10                        # top 10 from CSV only

python run.py search                                # search YouTube for "kpop songs"
python run.py search --q "blackpink songs"          # custom query, top 20
python run.py search --q "blackpink songs" --limit 10  # custom query, top 10
```

> **Note:** Video clips and databases are excluded from version control via `.gitignore` to avoid hitting GitHub file size limits.
