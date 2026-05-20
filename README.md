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

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/SongRanking.git
cd SongRanking
```

> **Note:** Video clips and databases are excluded from version control via `.gitignore` to avoid hitting GitHub file size limits.
