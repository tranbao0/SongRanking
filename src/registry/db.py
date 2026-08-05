"""
Local-working-copy-backed registry, synced with a hosted Turso (libSQL)
database: genre-tagged channels, their catalogued videos, song groupings
across duplicate uploads, and daily view-count snapshots. This is the
historical data store that chart computation (charts.py) reads from -
separate from data/songs.csv, which remains the render pipeline's own
per-chart working file.

Hosted rather than a local-only SQLite file: the registry used to live at
data/registry.db, inside this repo's OneDrive-synced folder. OneDrive gives
no real multi-writer safety for a single binary file - a delete on one
machine silently propagates to every synced machine - which cost a real
data-loss incident. See README's "Hosted registry database (Turso)"
section for one-time setup.

Reading/writing the hosted database directly, statement by statement,
turns out to be the wrong default though: every read/write becomes a real
network round trip, and sync's own write pattern is thousands of small
per-video/per-channel statements (see snapshot.py's deliberate per-row
commits, catalog.py's per-channel commits) - each one now paying network
latency it never used to. So get_connection() instead opens a *local*
working copy (LOCAL_WORKING_DB_PATH): every command that mutates the
registry (sync/decouple/regroup) calls pull_to_local() once at the start -
so a fresh run sees what other machines already pushed, and skips
re-spending YouTube/Gemini budget on channels the hosted registry already
has - does all of its own work at local-disk speed exactly like this
project did before it had a hosted db at all, runs grouping_audit.py's
headless agent against that same local file (no turso CLI/WSL relay
needed for that routine path - see scripts/turso_shell.sh for the rarer
manual/ad-hoc case of querying the hosted db directly), then
push_from_local() once at the very end, after everything - the sync
itself and any audit pass - is done.

The local working copy is gitignored scratch space, never the source of
truth - safe to delete between runs. If something eats it mid-run, the
worst case is redoing that one run's work from a fresh pull, not losing
history: the hosted copy is what stays durable.

Both pull_to_local() and push_from_local() default to replacing the
destination's contents outright rather than merging - correct as long as
nothing else writes to the hosted registry between this command's own
pull and push, the same single-writer-at-a-time assumption every other
destructive registry operation in this project already makes (see
decouple's confirmation prompt, the manual grouping doc's "run instances
one at a time"). That default (override=True) is what every internal
caller here uses (sync/decouple/regroup, grouping_audit's own pull+push
bracket), unchanged. `python run.py pull`/`push` instead default to
override=False (merge) - see run.py's --override flag - which upserts
by primary key (adds a row the destination is missing, overwrites a row
it already has with the source's current values) but never deletes a
row that only exists on the destination, for the manual case of
reconciling two copies without either one wiping out the other's data.

push_from_local()'s override=True is itself just that same upsert,
followed by a separate step that deletes whatever the hosted registry
still has that the upsert didn't touch - never a wholesale DELETE before
the rebuild starts. That ordering is what makes an override push safe to
interrupt at any point (dropped connection, killed process, sleeping
laptop): every already-committed chunk (see _push_chunk/_delete_chunk)
is either a row that should exist with correct values or a stale row
not yet cleaned up, never a row that's missing. This replaced an earlier
design that deleted every table up front and rebuilt it incrementally
over many separate round trips - safe only as long as nothing interrupted
the rebuild, and silently truncated the hosted registry with no way to
detect or recover from it otherwise when something did.
"""

import concurrent.futures
import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import turso_serverless

from shared import shutdown

_TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
_TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

LOCAL_WORKING_DB_PATH = Path(__file__).parent.parent.parent / "data" / ".registry_working.db"

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
]

_TABLES = ("channels", "songs", "videos", "view_snapshots", "rejected_videos")

# Primary key column(s) per table, used to build the ON CONFLICT target
# for merge mode's upserts (see pull_to_local/push_from_local). Must
# match _SCHEMA exactly - view_snapshots is the only composite key.
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


def get_remote_connection() -> turso_serverless.Connection:
    """
    Open a connection directly to the hosted Turso registry - the network,
    every-statement-is-a-round-trip path. Used only by pull_to_local() and
    push_from_local() themselves, and by low-volume read-only callers like
    charts.py that don't need a local working copy at all. Never called
    from sync/decouple/regroup's own per-channel/per-row work - see
    module docstring.
    """
    if not _TURSO_URL or not _TURSO_AUTH_TOKEN:
        raise EnvironmentError(
            "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are not set. Add them to .env - "
            "see README's \"Hosted registry database (Turso)\" section."
        )
    conn = turso_serverless.connect(_TURSO_URL, auth_token=_TURSO_AUTH_TOKEN)
    conn.row_factory = turso_serverless.Row
    try:
        _apply_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def get_connection() -> sqlite3.Connection:
    """
    Open a connection to the local working copy, creating the schema (and
    applying any pending column migrations) on first use. Row factory
    returns dict-like rows so callers can index by column name. This is
    what every registry-mutating command (sync/decouple/regroup) and
    grouping_audit.py's headless agent actually reads/writes - see module
    docstring for why, and call pull_to_local() first so it reflects the
    hosted registry's current state rather than being empty or stale.

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
    Copy the local working copy aside before pull_to_local() overwrites it,
    then delete all but the _LOCAL_BACKUP_KEEP most recent such backups.

    Same discipline as decouple.py's own pre-write backup, for the other
    operation that discards whatever is currently in the local working
    copy: a pull replaces it wholesale with the hosted registry's state,
    same as decouple's own changes replace what was there before them.
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


def _conflict_clause(table: str, columns: list[str]) -> str:
    """
    The ON CONFLICT clause shared by merge mode's local and remote
    upserts: a row `columns`/values apply to a primary key the
    destination already has overwrites that row's non-key columns with
    the incoming values (this is what makes merge actually carry
    changes to already-known rows, not just add new ones), while a
    primary key the destination doesn't have yet is a plain insert.
    Nothing on the destination is ever deleted by this - a row only the
    destination has is simply never mentioned in `rows`.
    """
    pk_cols = _PRIMARY_KEYS[table]
    update_cols = [c for c in columns if c not in pk_cols]
    if not update_cols:
        return f"ON CONFLICT({', '.join(pk_cols)}) DO NOTHING"
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    return f"ON CONFLICT({', '.join(pk_cols)}) DO UPDATE SET {set_clause}"


def _merge_insert_local(local: sqlite3.Connection, table: str, columns: list[str], rows: list[tuple]) -> None:
    """Upsert `rows` into the local working copy - see _conflict_clause."""
    placeholders = ", ".join("?" * len(columns))
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) {_conflict_clause(table, columns)}"
    local.executemany(sql, rows)


def _pk_set(conn, table: str) -> set[tuple]:
    """
    The full set of primary-key tuples currently in `table` on `conn` - a
    lightweight primary-key-only SELECT, one round trip if `conn` is
    remote. Used to diff local against remote in both directions' cleanup
    step: pull_to_local deletes local_pks - remote_pks (rows local has
    that the hosted registry doesn't), push_from_local deletes
    remote_pks - local_pks (the reverse).
    """
    pk_list = ", ".join(_PRIMARY_KEYS[table])
    return {tuple(row) for row in conn.execute(f"SELECT {pk_list} FROM {table}").fetchall()}


def _delete_local_rows(local: sqlite3.Connection, table: str, pk_cols: tuple[str, ...], pks: list[tuple]) -> None:
    """Delete `pks` from the local working copy's `table` - the cleanup
    half of an override pull, see pull_to_local. Unchunked/unthreaded
    unlike the remote-side equivalent (_delete_chunk): this is local disk,
    not a network round trip, so there's no batching benefit to chase."""
    if not pks:
        return
    where = " AND ".join(f"{c} = ?" for c in pk_cols)
    local.executemany(f"DELETE FROM {table} WHERE {where}", pks)


def pull_to_local(override: bool = True) -> None:
    """
    Bring the hosted registry's rows into the local working copy: every
    row the hosted registry has is upserted into local first (see
    _conflict_clause), then, only if override=True, whatever local still
    has that the hosted registry doesn't (by primary key) is deleted (see
    _pk_set). That order - and never deleting the local file itself -
    means an interruption at any point (dropped connection, killed
    process, sleeping laptop) leaves local either at its previous state
    or with a few stale rows not yet cleaned up, never empty or missing
    something it should have. This replaced an earlier design that
    deleted the local file outright and rebuilt it from nothing in one
    long-uncommitted pass - safe only because everything was wrapped in a
    single final commit, but an interruption anywhere before that commit
    (a crash, an outside kill - the same gap shutdown.critical_section()
    can't close, see shared/shutdown.py) still left local with nothing at
    all, not even what it had before the pull started.

    override=True (default - every internal caller here uses this,
    unchanged) is what makes this a full replace rather than a merge: the
    delete-orphans step runs, discarding whatever local-only, not-yet-
    pushed work existed - correct as long as nothing else writes to the
    hosted registry between this command's own pull and push (see the
    module docstring). Call once, before any of sync/decouple/regroup's
    own work - that's what lets a fresh run see whatever another machine
    already pushed, and skip re-spending YouTube/Gemini budget on
    channels the hosted registry already has (catalog.py's incremental
    per-channel logic needs to see their real last_catalog_sync/existing
    videos, not an empty local file). A timestamped backup of whatever
    the local working copy held is written first - see
    _backup_and_prune_local() - so a pull that clobbers not-yet-pushed
    local work is still recoverable.

    override=False stops after the upsert step - a row that only exists
    locally is left alone, merge never deletes, and the local file is
    never backed up either (nothing destructive happens to it). This is
    what `python run.py pull` uses by default (see run.py's --override
    flag).

    If the local file exists but isn't a readable SQLite database (this
    has happened for real - see data/.registry_working.db.corrupted-
    backup-before-restore from this project's own history), the upsert
    approach above has nothing to build on, so this falls back to the old
    discard-and-rebuild-from-nothing behavior for that one case: a
    corrupted file has no salvageable content regardless of override, so
    there's nothing an upsert could have preserved anyway.
    """
    if override:
        _backup_and_prune_local()
    remote = get_remote_connection()
    try:
        try:
            local = get_connection()
        except sqlite3.DatabaseError:
            if LOCAL_WORKING_DB_PATH.exists():
                LOCAL_WORKING_DB_PATH.unlink()
            local = get_connection()
        try:
            with shutdown.critical_section():
                for table in _TABLES:
                    cursor = remote.execute(f"SELECT * FROM {table}")
                    columns = [d[0] for d in cursor.description]
                    rows = [tuple(row) for row in cursor.fetchall()]
                    if rows:
                        _merge_insert_local(local, table, columns, rows)
                if override:
                    for table in _TABLES:
                        orphans = list(_pk_set(local, table) - _pk_set(remote, table))
                        _delete_local_rows(local, table, _PRIMARY_KEYS[table], orphans)
                local.commit()
        finally:
            local.close()
    finally:
        remote.close()


def _sql_literal(value) -> str:
    """
    Encode a Python value as a SQLite text literal, for the batched
    INSERT statements _push_rows builds. Only the value shapes this
    registry's columns actually hold (TEXT, INTEGER, NULL) are exercised;
    float/blob handling is included for completeness, not because a
    column here uses them.
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


# Rows per executescript() call in _push_chunk/_delete_chunk. Each chunk
# is one network round trip, so this trades a larger request body for
# fewer round trips - large enough that a full-registry push costs
# hundreds of requests rather than hundreds of thousands, small enough
# that one chunk's SQL text stays a reasonable HTTP payload size.
#
# turso_serverless.Connection.executemany() (confirmed in its own
# source: Cursor.executemany loops, calling _execute_stmt once per item)
# issues one HTTP request per row - for this registry's videos table
# alone, tens of thousands of sequential round trips. Values are inlined
# as SQL literals instead (via _sql_literal, which quotes/escapes text
# and never accepts anything from outside this codebase's own already-
# validated rows) and sent through executescript(), which - like
# _apply_schema's own use of it against this same remote connection type -
# sends its whole SQL text as a single pipelined request. That turns a
# chunk of _PUSH_CHUNK_SIZE rows into one round trip regardless of chunk
# size.
#
# The explicit BEGIN/COMMIT wrapping each chunk matters as much as the
# batching itself: measured directly, 500 INSERTs in one executescript()
# call without it took 8.0s (one commit - and its replication ack - per
# statement, inside the "sequence" pipeline type), against 0.22s with it
# (one commit for the whole chunk). The round-trip batching alone would
# still have paid that per-statement commit cost server-side.
_PUSH_CHUNK_SIZE = 500

# Worker threads for _run_chunked. Each chunk is an independent HTTP
# round trip, so this is I/O-bound and benefits from real concurrency
# despite the GIL - the interpreter releases it for the duration of each
# socket read/write. Kept modest rather than maximized: large enough
# that a full-registry push (hundreds of chunks for the videos/
# rejected_videos tables) finishes in a fraction of a sequential loop's
# time, small enough not to look like abuse to Turso's own rate limiting.
_PUSH_WORKERS = 8


def _run_chunked(jobs: list[tuple], worker) -> None:
    """
    Run `worker(remote_connection, *job)` for each job in `jobs` across
    _PUSH_WORKERS threads. Each thread gets its own hosted-registry
    connection, opened lazily on first use and reused for every later job
    that lands on that same thread - turso_serverless.Connection tracks a
    Hrana stream "baton" that must advance in request order, so two
    threads sharing one Connection would race and corrupt that stream.
    Capping at _PUSH_WORKERS connections (rather than one per job) also
    matters because opening one costs its own round trip (see
    get_remote_connection's _apply_schema call) - one per job would
    multiply request count instead of cutting it.

    Jobs are assumed independent of each other: nothing in this registry
    enforces foreign keys (never `PRAGMA foreign_keys = ON` on either
    connection type - see the module docstring), so neither which table a
    job belongs to nor the order jobs complete in affects correctness.
    The only ordering this codebase relies on is between *batches* of
    jobs, which is the caller's job: push_from_local doesn't start
    deleting orphaned rows until every upsert job across every table has
    already returned from its own _run_chunked call.
    """
    if not jobs:
        return
    connections: list = []
    lock = threading.Lock()
    thread_state = threading.local()

    def get_conn():
        conn = getattr(thread_state, "remote", None)
        if conn is None:
            conn = get_remote_connection()
            thread_state.remote = conn
            with lock:
                connections.append(conn)
        return conn

    def run(job):
        worker(get_conn(), *job)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_PUSH_WORKERS) as pool:
            # list(...) drains the iterator, which is what actually
            # re-raises a worker's exception here on the calling thread -
            # pool.map() alone would silently leave it unobserved.
            list(pool.map(run, jobs))
    finally:
        for conn in connections:
            conn.close()


def _push_chunk(remote, table: str, columns: list[str], chunk: list[tuple]) -> None:
    """Upsert one chunk of rows into `table` (see _conflict_clause) as a
    single pipelined request - see _PUSH_CHUNK_SIZE's comment for why
    this is batched and BEGIN/COMMIT-wrapped rather than one row or one
    statement per request."""
    conflict_clause = _conflict_clause(table, columns)
    inserts = "\n".join(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
        f"({', '.join(_sql_literal(v) for v in row)}) {conflict_clause};"
        for row in chunk
    )
    remote.executescript(f"BEGIN;\n{inserts}\nCOMMIT;")


def _delete_chunk(remote, table: str, pk_cols: tuple[str, ...], chunk: list[tuple]) -> None:
    """Delete one chunk of primary keys from `table` on the hosted
    registry - the cleanup half of an override push, see _pk_set/
    push_from_local. Chunked/threaded unlike the local-side equivalent
    (_delete_local_rows): this is a network round trip, so it gets the
    same batching treatment as _push_chunk."""
    if len(pk_cols) == 1:
        where = f"{pk_cols[0]} IN ({', '.join(_sql_literal(pk[0]) for pk in chunk)})"
    else:
        pk_list = ", ".join(pk_cols)
        values = ", ".join("(" + ", ".join(_sql_literal(v) for v in pk) + ")" for pk in chunk)
        where = f"({pk_list}) IN ({values})"
    remote.executescript(f"BEGIN;\nDELETE FROM {table} WHERE {where};\nCOMMIT;")


def push_from_local(override: bool = True) -> None:
    """
    Send the local working copy's rows to the hosted registry: every
    local row is upserted first (see _conflict_clause - a primary key the
    hosted registry doesn't have yet is added, one it already has gets
    its non-key columns overwritten with the local copy's current
    values), then, only if override=True, whatever the hosted registry
    still has that local doesn't (by primary key) is deleted - see
    _pk_set. Both steps run their many independent chunk
    requests across a small thread pool rather than one at a time - see
    _run_chunked.

    That upsert-then-delete order is what makes this safe to interrupt at
    any point (dropped connection, killed process, sleeping laptop, a
    stray Ctrl+C outside the critical sections below): every
    already-committed chunk is either a row that should exist with
    correct values, or a stale row not yet cleaned up - never a row
    that's missing. See the module docstring for why that matters.

    override=True (default - every internal caller here uses this,
    unchanged) is what makes this a full replace rather than a merge: the
    delete-orphans step runs. Call once, after all of sync/decouple/
    regroup's own work - including any grouping_audit pass - is done, so
    the hosted registry only ever settles into a fully-finished,
    already-audited state.

    override=False stops after the upsert step - a row that only exists
    on the hosted registry is left alone, merge never deletes. This is
    what `python run.py push` uses by default (see run.py's --override
    flag) - useful for reconciling two copies without either one wiping
    out data the other already has.
    """
    local = get_connection()
    try:
        jobs = []
        for table in _TABLES:
            cursor = local.execute(f"SELECT * FROM {table}")
            columns = [d[0] for d in cursor.description]
            rows = [tuple(row) for row in cursor.fetchall()]
            for i in range(0, len(rows), _PUSH_CHUNK_SIZE):
                jobs.append((table, columns, rows[i:i + _PUSH_CHUNK_SIZE]))
        with shutdown.critical_section():
            _run_chunked(jobs, _push_chunk)

        if override:
            remote = get_remote_connection()
            try:
                delete_jobs = []
                for table in _TABLES:
                    pk_cols = _PRIMARY_KEYS[table]
                    orphans = list(_pk_set(remote, table) - _pk_set(local, table))
                    for i in range(0, len(orphans), _PUSH_CHUNK_SIZE):
                        delete_jobs.append((table, pk_cols, orphans[i:i + _PUSH_CHUNK_SIZE]))
            finally:
                remote.close()
            with shutdown.critical_section():
                _run_chunked(delete_jobs, _delete_chunk)
    finally:
        local.close()
