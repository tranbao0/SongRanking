"""
SQLite-backed registry: genre-tagged channels, their catalogued videos,
song groupings across duplicate uploads, and daily view-count snapshots.
This is the historical data store that chart computation (charts.py) reads
from - separate from data/songs.csv, which remains the render pipeline's
own per-chart working file.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "registry.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id   TEXT PRIMARY KEY,
    genre        TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source       TEXT NOT NULL,
    source_ref   TEXT,
    added_date   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS songs (
    song_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      TEXT NOT NULL REFERENCES channels(channel_id),
    canonical_title TEXT NOT NULL,
    grouped_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL REFERENCES channels(channel_id),
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS view_snapshots (
    video_id      TEXT NOT NULL REFERENCES videos(video_id),
    snapshot_date TEXT NOT NULL,
    views         INTEGER NOT NULL,
    PRIMARY KEY (video_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
"""

# Created after _MIGRATIONS run, since one of them indexes a column
# (videos.song_id) that only exists on fresh databases via that migration.
#
# idx_snapshots_video is dropped rather than created: view_snapshots'
# PRIMARY KEY (video_id, snapshot_date) already has video_id as its leading
# column, so a separate index on video_id alone could never be chosen over
# it - confirmed with EXPLAIN QUERY PLAN, which uses the primary key's
# implicit index for every query in this codebase. It only ever cost write
# time on each day's snapshot insert.
#
# What was actually missing is the reverse: snapshot.take_snapshot filters
# by snapshot_date alone to find videos still pending for today, which the
# primary key can't serve (wrong column order) and which was therefore a
# full table scan growing by one row per video per day.
_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_videos_song ON videos(song_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON view_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_channels_genre ON channels(genre);
DROP INDEX IF EXISTS idx_snapshots_video;
"""

# Columns added after the initial release - ALTER TABLE ADD COLUMN doesn't
# have an IF NOT EXISTS form, so each is applied individually and the
# "duplicate column" error (already-migrated databases) is swallowed.
_MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN song_id INTEGER REFERENCES songs(song_id)",
    "ALTER TABLE channels ADD COLUMN last_catalog_sync TEXT",
    "ALTER TABLE channels ADD COLUMN last_known_video_count INTEGER",
]


def get_connection() -> sqlite3.Connection:
    """
    Open a connection to data/registry.db, creating the schema (and
    applying any pending column migrations) on first use. Row factory
    returns dict-like rows so callers can index by column name.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    for migration in _MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
    conn.executescript(_POST_MIGRATION_INDEXES)
    conn.commit()
    return conn
