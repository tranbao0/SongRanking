"""
Puts src/ on sys.path so tests import the same way run.py does, and gives
tests a throwaway registry database.

Nothing here touches data/registry.db or any real API. Every test builds
its own temporary database, and no test imports a module at a point where
it would make a network call - the goal is that `python -m unittest` is
always safe to run mid-sync.
"""

import sqlite3
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class KeepOpenConnection(sqlite3.Connection):
    """
    A connection whose close() is a no-op, for testing the sync entry
    points - they own their connection and close it in a finally block,
    which would otherwise discard an in-memory database before the test
    could assert against it. Connection.close is read-only, so this has to
    be a subclass rather than a patched attribute.
    """

    def close(self):
        pass

    def really_close(self):
        super().close()


def make_db(keep_open: bool = False, check_same_thread: bool = True) -> sqlite3.Connection:
    """
    An in-memory registry with the same schema get_connection() produces,
    including the migrated columns. Built by replaying db.py's own schema
    statements rather than by copying them, so a schema change there is
    picked up here automatically.

    check_same_thread=False is for tests standing this in as a mocked
    get_remote_connection() against db.push_from_local(): that function's
    _run_chunked dispatches each chunk to its own worker thread (see
    db.py), so a test remote connection needs to tolerate being used from
    a thread other than the one that created it - safe here because
    SQLite's own default build is thread-safe ("serialized" mode), which
    is exactly what this flag exists to let a caller opt into.
    """
    from registry import db

    conn = sqlite3.connect(
        ":memory:",
        check_same_thread=check_same_thread,
        factory=KeepOpenConnection if keep_open else sqlite3.Connection,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    for migration in db._MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
    conn.executescript(db._POST_MIGRATION_INDEXES)
    conn.commit()
    return conn


def add_channel(conn, channel_id, genre="kpop", display_name=None, source="wikidata"):
    conn.execute(
        "INSERT INTO channels (channel_id, genre, display_name, source, source_ref, added_date) "
        "VALUES (?, ?, ?, ?, NULL, '2026-01-01')",
        (channel_id, genre, display_name or channel_id, source),
    )


def add_video(conn, video_id, channel_id, title=None, published_at="2024-01-01T00:00:00Z", song_id=None):
    conn.execute(
        "INSERT INTO videos (video_id, channel_id, title, url, published_at, discovered_at, song_id) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-01', ?)",
        (video_id, channel_id, title or video_id,
         f"https://www.youtube.com/watch?v={video_id}", published_at, song_id),
    )


def add_snapshot(conn, video_id, snapshot_date, views):
    conn.execute(
        "INSERT INTO view_snapshots (video_id, snapshot_date, views) VALUES (?, ?, ?)",
        (video_id, snapshot_date, views),
    )
