"""
Local-working-copy-backed registry, synced with a private git repo (the
"data repo"): genre-tagged channels, their catalogued videos, song
groupings across duplicate uploads, and daily view-count snapshots. This
is the historical data store that chart computation (charts.py) reads
from - separate from data/songs.csv, which remains the render pipeline's
own per-chart working file.

Git-synced rather than a single local SQLite file: the registry used to
live at data/registry.db, inside this repo's OneDrive-synced folder, which
gives no real multi-writer safety for a single binary file (a delete on
one machine silently propagates to every synced machine) - that cost a
real data-loss incident. It then moved to a hosted Turso (libSQL)
database, which fixed that but made every statement a network round trip
(too slow at this project's scale) and cost a paid hosted service for
something that only ever has one writer.

The data repo (REGISTRY_DATA_REPO_URL) holds the registry as deterministic
SQL text, not a binary database file - see DATA_REPO_PATH's layout below.
Text diffs/compresses far better than a SQLite file's binary layout (which
re-diffs almost entirely on nearly any change), and lets the owner's daily
push double as real version control (git log/diff/revert over readable
INSERT statements) without ever committing a binary blob. `channels`,
`songs`, and `videos` stay small (low tens of thousands of rows) and are
fully re-dumped on every push - simple, and cheap enough to redo every
time. `rejected_videos` and `view_snapshots` are the two tables with real
long-term growth (hundreds of thousands of rows and climbing by design),
so those are append-only chunk files (one per calendar month, keyed off
each row's own date column) - a routine push only touches the current
month's file, never rewrites history, which is what keeps git's history
growing proportional to new content rather than to total size. See
_FULL_DUMP_TABLES/_APPEND_TABLES below.

`sync`/`decouple`/`regroup` don't read/write the data repo statement by
statement - `pull_to_local()` materializes a *local* working copy
(LOCAL_WORKING_DB_PATH) from the data repo's current files once, at the
start; every command that mutates the registry does its own work against
that local file at local-disk speed; `push_from_local()` re-derives the
data repo's files from that local file's current state once, at the end,
and commits+pushes them. This is the same shape the Turso-backed design
used, for the same reason (thousands of small per-video/per-channel
writes would each cost a git operation otherwise) - only the transport
underneath changed.

Both pull and push are always full operations now, not a merge/override
choice: pull_to_local() always rebuilds the local working copy from
scratch from whatever the data repo currently holds, and push_from_local()
always publishes the local working copy's complete current state. The
old merge mode existed to reconcile two independently-writable copies
without either one silently discarding the other's data - unnecessary
now that there is exactly one writer (this project's owner) and the git
repo is unambiguously the single source of truth. `python run.py pull`/
`push` no longer take an --override flag; see run.py.
"""

import os
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from shared import shutdown

DATA_REPO_URL = os.environ.get("REGISTRY_DATA_REPO_URL")

LOCAL_WORKING_DB_PATH = Path(__file__).parent.parent.parent / "data" / ".registry_working.db"

# The data repo's local checkout - its own separate git repository, not
# tracked by this one (see .gitignore). Persists between runs so a pull
# is normally an incremental `git pull`, not a fresh clone.
DATA_REPO_PATH = Path(__file__).parent.parent.parent / "data" / ".registry-data"

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

-- Uploads catalog.py's mv_filter rejected (title didn't name the song as
-- the focal point, or duration fell outside the MV window) - never a song,
-- so they're kept out of `videos` entirely rather than living there with
-- song_id NULL. That NULL used to be ambiguous with the state decouple.py
-- leaves a real, previously-grouped video in, which is exactly what let
-- regroup.py's "song_id NULL means needs grouping" assumption silently
-- re-admit already-rejected uploads as songs. This table exists only so a
-- channel's next sync can still recognize a rejected video as already-seen
-- and stop paginating there, without needing a `videos` row to do it.
CREATE TABLE IF NOT EXISTS rejected_videos (
    video_id      TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL REFERENCES channels(channel_id),
    title         TEXT NOT NULL,
    checked_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_rejected_videos_channel ON rejected_videos(channel_id);
"""

_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_videos_song ON videos(song_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON view_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_channels_genre ON channels(genre);
CREATE INDEX IF NOT EXISTS idx_songs_channel ON songs(channel_id);
DROP INDEX IF EXISTS idx_snapshots_video;
"""

# Columns added after the initial release - ALTER TABLE ADD COLUMN doesn't
# have an IF NOT EXISTS form, so each is applied individually and the
# "duplicate column" error (already-migrated databases) is swallowed.
_MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN song_id INTEGER REFERENCES songs(song_id)",
    "ALTER TABLE channels ADD COLUMN last_catalog_sync TEXT",
    "ALTER TABLE channels ADD COLUMN last_known_video_count INTEGER",
    # JSON-encoded list of the uploader's own creator-supplied tags, empty/
    # NULL for a video catalogued before this column existed. See
    # song_grouping's module docstring for why this only ever feeds the AI
    # tier's context, never a deterministic tier's matching key.
    "ALTER TABLE videos ADD COLUMN tags TEXT",
]

# Small, routinely-mutated tables (channels.last_catalog_sync bumps every
# sync; videos.song_id changes via regroup/decouple) - fully re-dumped to
# a single file on every push. Ordered by primary key so an unchanged row
# produces a byte-identical line, letting git's own line diff carry the
# "what actually changed" signal instead of a custom diff needing to.
_FULL_DUMP_TABLES = ("channels", "songs", "videos")

# Large, append-heavy tables - see module docstring. Never deleted from in
# normal operation; only discovery.py's _purge_excluded (rare, manual
# channel-exclusion) ever removes rows, handled by _needs_full_regen.
_APPEND_TABLES = ("rejected_videos", "view_snapshots")

# The date column each append table's chunk files are bucketed by (one
# file per calendar month of this column's value) - not "today", so a
# push after a multi-day gap still files rows under the month they
# actually belong to rather than lumping them into the push date's month.
_APPEND_DATE_COLUMN = {"rejected_videos": "checked_at", "view_snapshots": "snapshot_date"}

_TABLES = _FULL_DUMP_TABLES + _APPEND_TABLES

# Primary key column(s) per table - used for full-dump ORDER BY and as
# the tie-breaker ordering within a regenerated append-table chunk file.
# Must match _SCHEMA exactly - view_snapshots is the only composite key.
_PRIMARY_KEYS = {
    "channels": ("channel_id",),
    "songs": ("song_id",),
    "videos": ("video_id",),
    "view_snapshots": ("video_id", "snapshot_date"),
    "rejected_videos": ("video_id",),
}

# How many pre-pull backups pull_to_local() keeps around. Each is a full
# copy of the local working copy (tens of MB), taken every pull, so this
# caps disk use rather than letting them accumulate forever - 3 is enough
# to recover from "that last pull clobbered local work I meant to push"
# without needing to reach back further than the last couple of runs.
_LOCAL_BACKUP_KEEP = 3


def _apply_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    for migration in _MIGRATIONS:
        try:
            conn.execute(migration)
        except Exception as e:
            if "duplicate column" not in str(e):
                raise
    conn.executescript(_POST_MIGRATION_INDEXES)
    conn.commit()


def get_connection() -> sqlite3.Connection:
    """
    Open a connection to the local working copy, creating the schema (and
    applying any pending column migrations) on first use. Row factory
    returns dict-like rows so callers can index by column name. This is
    what every registry-mutating command (sync/decouple/regroup) and
    grouping_audit.py's headless agent actually reads/writes, and also
    what charts.py reads from - call pull_to_local() first if you want it
    to reflect the data repo's latest pushed state rather than whatever
    was last pulled.

    If the file exists but isn't a readable SQLite database, schema
    application raises - explicitly closed before that propagates rather
    than left for garbage collection, since on Windows an unclosed
    sqlite3.Connection still holds the underlying file open, and
    pull_to_local's corrupted-file fallback needs to unlink() this same
    path immediately after catching that error.
    """
    LOCAL_WORKING_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOCAL_WORKING_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        _apply_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _backup_and_prune_local() -> None:
    """
    Copy the local working copy aside before pull_to_local() rebuilds it,
    then delete all but the _LOCAL_BACKUP_KEEP most recent such backups.
    """
    if not LOCAL_WORKING_DB_PATH.exists():
        return
    backup = LOCAL_WORKING_DB_PATH.parent / f"registry.{datetime.now().strftime('%Y%m%d-%H%M%S')}.pre-pull.db"
    shutil.copy2(LOCAL_WORKING_DB_PATH, backup)

    # Timestamp format sorts lexicographically in chronological order, so
    # the oldest backups are just the leading entries of the sorted glob.
    backups = sorted(LOCAL_WORKING_DB_PATH.parent.glob("registry.*.pre-pull.db"))
    for stale in backups[:-_LOCAL_BACKUP_KEEP]:
        stale.unlink()


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def _sync_data_repo() -> None:
    """
    Bring DATA_REPO_PATH's checkout up to date: clone it if it doesn't
    exist yet, otherwise a plain fast-forward pull. --ff-only rather than
    a merge/rebase pull is deliberate - this checkout is only ever
    fast-forwarded by this same pull, or advanced by this machine's own
    push; a real divergence would mean something unexpected happened to
    the checkout (or the single-writer assumption was violated), which
    should surface as an error here rather than silently merge.
    """
    if not DATA_REPO_URL:
        raise EnvironmentError(
            "REGISTRY_DATA_REPO_URL is not set. Add it to .env - see README's "
            "\"Git-synced registry database\" section."
        )
    if (DATA_REPO_PATH / ".git").is_dir():
        _run_git("pull", "--ff-only", cwd=DATA_REPO_PATH)
    else:
        DATA_REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
        _run_git("clone", DATA_REPO_URL, str(DATA_REPO_PATH), cwd=DATA_REPO_PATH.parent)


def _sql_literal(value) -> str:
    """
    Encode a Python value as a SQLite text literal, for the INSERT
    statements this module writes into the data repo's dump files. Only
    the value shapes this registry's columns actually hold (TEXT,
    INTEGER, NULL) are exercised; float/blob handling is included for
    completeness, not because a column here uses them.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "x'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _insert_line(table: str, columns: list[str], values: tuple) -> str:
    cols = ", ".join(columns)
    vals = ", ".join(_sql_literal(v) for v in values)
    return f"INSERT INTO {table} ({cols}) VALUES ({vals});"


def _load_dump_file(conn: sqlite3.Connection, path: Path) -> None:
    """
    Load one dump file's INSERT statements into `conn`, wrapped in a
    single explicit transaction. Without this, executescript() commits
    each statement individually (Python's sqlite3 module only commits a
    *pending* transaction before running a script, it doesn't wrap the
    script's own statements in one) - for a chunk file with hundreds of
    thousands of rows, that's hundreds of thousands of individual fsyncs.
    Measured directly: this is the same fix the old Turso-era code made
    for its own per-chunk network pushes (see this module's history),
    just missed here on the file-loading side - a few seconds instead of
    several minutes for this registry's largest table.
    """
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8").strip()
    if text:
        conn.executescript(f"BEGIN;\n{text}\nCOMMIT;")


def pull_to_local() -> None:
    """
    Rebuild the local working copy from scratch from the data repo's
    current files: pull the data repo's checkout, back up whatever the
    local working copy currently holds (see _backup_and_prune_local - a
    pull discards not-yet-pushed local work, so this is what makes that
    recoverable), then replay channels.sql/songs.sql/videos.sql followed
    by every rejected_videos/*.sql and view_snapshots/*.sql chunk file (in
    filename order, which is chronological - see _APPEND_DATE_COLUMN) into
    a fresh schema.

    Always a full rebuild - see module docstring for why the old
    merge/override distinction no longer applies. Call once, before any
    of sync/decouple/regroup's own work, so a fresh run sees whatever was
    last pushed and skips re-spending YouTube/Gemini budget on channels
    the data repo already has current data for.
    """
    _sync_data_repo()
    _backup_and_prune_local()
    if LOCAL_WORKING_DB_PATH.exists():
        LOCAL_WORKING_DB_PATH.unlink()

    local = get_connection()
    try:
        with shutdown.critical_section():
            for table in _FULL_DUMP_TABLES:
                _load_dump_file(local, DATA_REPO_PATH / f"{table}.sql")
            for table in _APPEND_TABLES:
                table_dir = DATA_REPO_PATH / table
                if table_dir.is_dir():
                    for chunk in sorted(table_dir.glob("*.sql")):
                        _load_dump_file(local, chunk)
            local.commit()
    finally:
        local.close()


def _dump_table_full(conn: sqlite3.Connection, table: str, path: Path) -> None:
    pk_cols = _PRIMARY_KEYS[table]
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {', '.join(pk_cols)}")
    columns = [d[0] for d in cursor.description]
    lines = [_insert_line(table, columns, tuple(row)) for row in cursor.fetchall()]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _existing_append_pks(table: str, table_dir: Path) -> set[tuple]:
    """
    The set of primary keys already recorded in `table`'s chunk files, by
    actually replaying those files (the same way pull_to_local() does)
    into a scratch in-memory database rather than parsing the SQL text by
    hand - correct by construction, since it's the exact same INSERT
    statements SQLite itself will later execute for real.
    """
    if not table_dir.is_dir():
        return set()
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    try:
        scratch.executescript(_SCHEMA)
        for chunk in sorted(table_dir.glob("*.sql")):
            _load_dump_file(scratch, chunk)
        pk_cols = _PRIMARY_KEYS[table]
        return {tuple(row) for row in scratch.execute(f"SELECT {', '.join(pk_cols)} FROM {table}")}
    finally:
        scratch.close()


def _regenerate_append_table(conn: sqlite3.Connection, table: str, table_dir: Path) -> None:
    """
    Rebuild every chunk file for `table` from its current local state,
    replacing whatever chunk files exist - the correctness-over-cleverness
    fallback _sync_append_table takes when a previously-recorded row is no
    longer present locally (only possible via discovery.py's
    _purge_excluded, a rare, manual channel-exclusion cascade).
    """
    date_col = _APPEND_DATE_COLUMN[table]
    pk_cols = _PRIMARY_KEYS[table]

    if table_dir.is_dir():
        for stale in table_dir.glob("*.sql"):
            stale.unlink()

    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {date_col}, {', '.join(pk_cols)}")
    columns = [d[0] for d in cursor.description]
    by_month: dict[str, list[tuple]] = defaultdict(list)
    for row in cursor.fetchall():
        by_month[row[date_col][:7]].append(tuple(row))

    if by_month:
        table_dir.mkdir(parents=True, exist_ok=True)
    for month, rows in by_month.items():
        lines = [_insert_line(table, columns, v) for v in rows]
        (table_dir / f"{month}.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sync_append_table(conn: sqlite3.Connection, table: str, table_dir: Path) -> None:
    """
    Bring `table`'s chunk files up to date with the local working copy's
    current state: append whatever primary keys exist locally but aren't
    in the chunk files yet (see _existing_append_pks) to their date
    column's chunk file, without touching existing content - unless a
    primary key the chunk files already have is now missing locally, in
    which case that's a deletion the append-only path can't represent
    (see _regenerate_append_table's docstring), so every chunk file is
    rebuilt from scratch instead.

    Diffing full primary-key sets against each other, rather than
    tracking a watermark, is deliberate: SQLite's implicit rowid is *not*
    a safe append-order marker here - it's reassigned as "one more than
    the current max in the table", so deleting the highest-rowid row and
    inserting a new one can silently reuse that same rowid. A primary-key
    set diff has no such failure mode, and at this registry's actual
    scale (hundreds of thousands of rows, entirely local/in-memory, no
    network) is fast enough to just do on every push.
    """
    pk_cols = _PRIMARY_KEYS[table]
    date_col = _APPEND_DATE_COLUMN[table]
    existing = _existing_append_pks(table, table_dir)

    cursor = conn.execute(f"SELECT * FROM {table}")
    columns = [d[0] for d in cursor.description]
    pk_idx = [columns.index(c) for c in pk_cols]
    date_idx = columns.index(date_col)

    current_pks = set()
    new_rows = []
    for row in cursor.fetchall():
        values = tuple(row)
        pk = tuple(values[i] for i in pk_idx)
        current_pks.add(pk)
        if pk not in existing:
            new_rows.append(values)

    if existing - current_pks:
        _regenerate_append_table(conn, table, table_dir)
        return
    if not new_rows:
        return

    by_month: dict[str, list[tuple]] = defaultdict(list)
    for values in new_rows:
        by_month[values[date_idx][:7]].append(values)

    table_dir.mkdir(parents=True, exist_ok=True)
    for month, month_rows in by_month.items():
        lines = [_insert_line(table, columns, v) for v in month_rows]
        with (table_dir / f"{month}.sql").open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def push_from_local() -> None:
    """
    Publish the local working copy's complete current state to the data
    repo: fully re-dump channels/songs/videos, append-or-regenerate
    rejected_videos/view_snapshots (see _sync_append_table), then commit
    and push - inside a single shutdown.critical_section() (a git commit
    is itself transactional, so this only guards the narrower window
    between commit and push: a Ctrl+C there leaves an already-valid local
    commit simply not yet on the remote, which the next push carries
    along harmlessly).

    Always a full publish - see module docstring for why the old
    merge/override distinction no longer applies. Call once, after all of
    sync/decouple/regroup's own work - including any grouping_audit pass
    - is done.
    """
    _sync_data_repo()
    local = get_connection()
    try:
        for table in _FULL_DUMP_TABLES:
            _dump_table_full(local, table, DATA_REPO_PATH / f"{table}.sql")
        for table in _APPEND_TABLES:
            _sync_append_table(local, table, DATA_REPO_PATH / table)

        with shutdown.critical_section():
            _run_git("add", "-A", cwd=DATA_REPO_PATH)
            status = _run_git("status", "--porcelain", cwd=DATA_REPO_PATH)
            if not status.stdout.strip():
                return
            timestamp = datetime.now().isoformat(timespec="seconds")
            _run_git("commit", "-m", f"registry update {timestamp}", cwd=DATA_REPO_PATH)
            # -u origin HEAD (rather than a bare `git push`) so the very
            # first push from a freshly-cloned empty data repo doesn't fail
            # with "no upstream branch" - harmless to repeat on every push
            # after that, since it's already set from then on.
            _run_git("push", "-u", "origin", "HEAD", cwd=DATA_REPO_PATH)
    finally:
        local.close()
