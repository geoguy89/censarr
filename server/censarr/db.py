"""Job state. SQLite, one file, no ORM."""

from __future__ import annotations

from . import env

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .subtitles import LOW_CONFIDENCE

def _default_db() -> Path:
    """The database under the new name, or the one an install made as Cleanarr."""
    old = Path("/config/cleanarr.sqlite")
    return old if old.exists() else Path("/config/censarr.sqlite")


DB_PATH = Path(env.get("DB")) if env.get("DB") else _default_db()
_local = threading.local()

# queued -> running -> done | failed | skipped | cancelled
SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,             -- episode | movie | file
  title       TEXT NOT NULL,             -- what a person would call it
  subtitle    TEXT DEFAULT '',           -- S02E04, or the year
  path        TEXT NOT NULL,
  source      TEXT DEFAULT '',           -- sonarr | plex | jellyfin | manual | radarr (its webhook, and older jobs)
  source_id   TEXT DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'queued',
  stage       TEXT DEFAULT '',
  progress    REAL DEFAULT 0,
  message     TEXT DEFAULT '',
  muted       INTEGER DEFAULT 0,
  duration    REAL DEFAULT 0,
  -- Clean a file that already has a cleaned track, replacing it. Used after
  -- the word list changes, or after a wrong call is corrected.
  force       INTEGER DEFAULT 0,
  created_at  REAL NOT NULL,
  started_at  REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS job_status ON job (status, id);
CREATE UNIQUE INDEX IF NOT EXISTS job_path_open
  ON job (path) WHERE status IN ('queued', 'running');

-- Shows whose future episodes should be cleaned without being asked for.
-- Marked automatically when a season or an episode is cleaned, and turned off
-- again from the show's page.
CREATE TABLE IF NOT EXISTS monitor (
  source       TEXT NOT NULL,          -- sonarr, or films for the one film switch
  source_id    TEXT NOT NULL,          -- the series id (films: new)
  title        TEXT DEFAULT '',
  added_at     REAL NOT NULL,
  last_checked REAL DEFAULT 0,
  PRIMARY KEY (source, source_id)
);

-- Every film the media server has listed since "Censor new films" went on. A
-- film not in here is a new one. Emptied when the switch goes off, so
-- switching it on again starts from the library as it stands then.
CREATE TABLE IF NOT EXISTS film_seen (
  path TEXT PRIMARY KEY
);

-- Answers the second opinion has already given. The same word in the same
-- sentence has the same answer forever, and each question costs 30-100
-- seconds of a model thinking - so re-cleaning a file after a word-list change
-- should not ask them all again.
CREATE TABLE IF NOT EXISTS judge_cache (
  key        TEXT PRIMARY KEY,
  verdict    TEXT NOT NULL,
  instead    TEXT DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS detection (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id   INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  start    REAL NOT NULL,
  end      REAL NOT NULL,
  text     TEXT NOT NULL,
  category TEXT NOT NULL,
  review   INTEGER DEFAULT 0,
  reason   TEXT DEFAULT '',
  -- 0 means found but deliberately left in: the second opinion read it as an
  -- ordinary word. Kept so a wrong call can be seen rather than guessed at.
  muted    INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS detection_job ON detection (job_id, start);
"""


# The schema and the migrations are a WRITE, and they only need doing once per
# process - but connections are per-thread, and this service has a lot of
# threads: the worker, the poster warmers, the library pool, and one per
# in-flight request. Running CREATE TABLE and ALTER TABLE on every one of them
# meant every new thread grabbed the write lock just to say hello, competing
# with a job that was busy recording detections. That is what "database is
# locked" was: not a long transaction, but a crowd of short ones.
_schema_ready = False
_schema_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    global _schema_ready
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Safe under WAL: a commit still goes to the write-ahead log, it just
        # does not fsync on every one. Shortens the window each write holds the
        # lock, which is the thing that was hurting here. The cost is that a
        # sudden power cut can lose the last few commits - acceptable for a
        # queue that can re-clean a file.
        conn.execute("PRAGMA synchronous=NORMAL")
        # Anything that keeps a reader open is asked to get out of the way
        # rather than fail immediately.
        conn.execute("PRAGMA busy_timeout=30000")

        with _schema_lock:
            if not _schema_ready:
                conn.executescript(SCHEMA)
                _migrate(conn)
                _schema_ready = True

        _local.conn = conn
    return conn


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", and CREATE TABLE IF NOT EXISTS silently leaves an older table
# alone - which is how a database ends up missing a column the code needs.
_ADDED_COLUMNS = (
    ("job", "force", "INTEGER DEFAULT 0"),
    ("detection", "muted", "INTEGER DEFAULT 1"),
    # catch_up: clean everything this show already has. new_only: leave the
    # back catalogue alone and clean what arrives from now on.
    ("monitor", "mode", "TEXT DEFAULT 'catch_up'"),
    # Where a job sits in the queue. Lower runs sooner. A float so a job can
    # be moved between two others without renumbering the whole queue.
    ("job", "position", "REAL DEFAULT 0"),
    # clean | remove. A removal is queued like any other job so it is
    # serialised, visible, and cannot run while the same file is being cleaned.
    ("job", "action", "TEXT DEFAULT 'clean'"),
    # Size of the cleaned track this job added, so the Cleaned page can say
    # what all of this is costing in disk.
    ("job", "added_bytes", "INTEGER DEFAULT 0"),
    # Which word a remembered answer was about, so "is this check earning its
    # keep?" can be answered from the data instead of argued about.
    ("judge_cache", "word", "TEXT DEFAULT ''"),
    # How sure Whisper was of a detected word, and what the subtitles said at
    # that moment - evidence for a person reviewing it, never for muting.
    ("detection", "confidence", "REAL"),
    ("detection", "subtitle", "TEXT DEFAULT ''"),
    ("detection", "subtitle_state", "TEXT DEFAULT ''"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, spec in _ADDED_COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    conn.commit()


def _queue_edge(front: bool) -> float:
    """A position just before the first queued job, or just after the last."""
    row = connect().execute(
        "SELECT MIN(position) AS lo, MAX(position) AS hi FROM job "
        "WHERE status IN ('queued','running')").fetchone()
    lo, hi = row["lo"], row["hi"]
    if lo is None:
        return 0.0
    return (lo - 1.0) if front else (hi + 1.0)


def enqueue(kind: str, title: str, path: str, subtitle: str = "",
            source: str = "manual", source_id: str = "",
            force: bool = False, front: bool = False,
            action: str = "clean") -> int | None:
    """Returns the new job id, or None if that file is already waiting.

    The unique index does the de-duplicating: queue a season twice and the
    second attempt adds nothing rather than cleaning every episode twice.
    """
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO job (kind, title, subtitle, path, source, source_id, force,"
            " position, action, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kind, title, subtitle, path, source, source_id, int(force),
             _queue_edge(front), action, time.time()))
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def requeue_interrupted() -> int:
    """Put jobs that were mid-flight back in the queue, at startup.

    A job is only ever marked done after the new file has been verified and
    swapped in, and every intermediate file lives in a scratch directory that
    is deleted either way - so a job interrupted by a restart or a power cut
    has changed nothing, and simply running it again is correct. Left alone it
    would sit at "running" forever, because the worker only ever looks for
    queued work.
    """
    conn = connect()
    cur = conn.execute(
        "UPDATE job SET status='queued', progress=0, stage='', "
        "message='restarted - the service stopped part way through' "
        "WHERE status='running'")
    conn.commit()
    return cur.rowcount


def next_queued() -> sqlite3.Row | None:
    return connect().execute(
        "SELECT * FROM job WHERE status='queued' "
        "ORDER BY position, id LIMIT 1").fetchone()


def queued_in_order() -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT id, position FROM job WHERE status='queued' "
        "ORDER BY position, id").fetchall()


def move(job_id: int, where: str) -> bool:
    """Reorder one queued job: to the front, to the back, or one step either way."""
    return move_many([job_id], where)


def move_many(job_ids: list[int], where: str) -> int:
    """Move a set of queued jobs together, keeping their order among themselves.

    Selecting a whole season and pressing up should move the season, not
    thirty individual rows - so the selection is treated as one block. For a
    scattered selection that means the chosen rows end up together, which is
    the only reading of "move these up" that does not need explaining.
    """
    order = [r["id"] for r in queued_in_order()]
    wanted = {int(i) for i in job_ids}
    chosen = [i for i in order if i in wanted]          # in queue order, not click order
    rest = [i for i in order if i not in wanted]
    if not chosen:
        return 0

    first = order.index(chosen[0])
    last = order.index(chosen[-1])

    if where == "top":
        new_order = chosen + rest
    elif where == "bottom":
        new_order = rest + chosen
    elif where == "up":
        # Slot in above the nearest row above the block that is not selected.
        above = [i for i in order[:first] if i not in wanted]
        if not above:
            return 0
        at = rest.index(above[-1])
        new_order = rest[:at] + chosen + rest[at:]
    elif where == "down":
        below = [i for i in order[last + 1:] if i not in wanted]
        if not below:
            return 0
        at = rest.index(below[0])
        new_order = rest[:at + 1] + chosen + rest[at + 1:]
    else:
        return 0

    # Renumber the whole queue from the new order. It is tens of rows, and
    # simple beats clever when the alternative is juggling fractions.
    conn = connect()
    conn.executemany("UPDATE job SET position=? WHERE id=?",
                     [(float(i), job) for i, job in enumerate(new_order)])
    conn.commit()
    return len(chosen)


def retry_failed(job_ids: list[int] | None = None,
                 statuses: tuple[str, ...] = ("failed", "cancelled")) -> int:
    """Put failed or cancelled jobs back in the queue, at the back.

    Neither leaves a mark on the file - nothing is changed until the verified
    swap at the end - so putting one back is always safe, whatever happened.
    Cancelled is included because "I stopped that, actually put it back" is the
    most common reason to look at the queue at all.
    """
    conn = connect()
    tail = _queue_edge(front=False)
    marks_status = ",".join("?" * len(statuses))
    if job_ids:
        marks = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"SELECT id FROM job WHERE status IN ({marks_status}) AND id IN ({marks})",
            [*statuses, *[int(i) for i in job_ids]]).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id FROM job WHERE status IN ({marks_status}) ORDER BY id",
            list(statuses)).fetchall()
    for offset, row in enumerate(rows):
        conn.execute(
            "UPDATE job SET status='queued', progress=0, stage='', message='retrying',"
            " position=?, started_at=NULL, finished_at=NULL WHERE id=?",
            (tail + offset, row["id"]))
    conn.commit()
    return len(rows)


def cancel_many(job_ids: list[int]) -> int:
    """Cancel a set of queued jobs. A job already running is left alone."""
    if not job_ids:
        return 0
    conn = connect()
    marks = ",".join("?" * len(job_ids))
    cur = conn.execute(
        f"UPDATE job SET status='cancelled', message='cancelled' "
        f"WHERE status='queued' AND id IN ({marks})", [int(i) for i in job_ids])
    conn.commit()
    return cur.rowcount


def update(job_id: int, **fields) -> None:
    if not fields:
        return
    conn = connect()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE job SET {sets} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


def get(job_id: int) -> sqlite3.Row | None:
    return connect().execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()


# What the Queue page is for: work outstanding. Finished jobs live on the
# Cleaned page and cancelled ones are nobody's business - leaving either here
# buries the handful of rows that actually need looking at. Failures stay,
# because a failure is outstanding work that needs a person.
# What the Queue page shows. Cancelled belongs here: the page offers to
# "Remove 1 cancelled", and being told something was cancelled while being
# shown neither what it was nor a way to put it back is worse than not
# mentioning it. A cancelled job is usually one somebody wants to reinstate.
QUEUE_STATUSES = ("running", "queued", "failed", "cancelled")


def recent(limit: int = 100, status: str | None = None,
           include_finished: bool = False) -> list[sqlite3.Row]:
    """The queue view: what is running, what is waiting, and what went wrong."""
    sql = "SELECT * FROM job"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    elif not include_finished:
        marks = ",".join("?" * len(QUEUE_STATUSES))
        sql += f" WHERE status IN ({marks})"
        args.extend(QUEUE_STATUSES)
    # Running first, then the queue in the order it will actually run, then
    # everything finished, newest first.
    sql += (" ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,"
            " CASE WHEN status='queued' THEN position END,"
            " CASE WHEN status='queued' THEN id ELSE -id END LIMIT ?")
    args.append(limit)
    return connect().execute(sql, args).fetchall()


def save_detections(job_id: int, muted, left_in=()) -> None:
    conn = connect()
    conn.execute("DELETE FROM detection WHERE job_id=?", (job_id,))

    def row(m, review, was_muted):
        return (job_id, m.start, m.end, m.text, m.category, review, m.reason, was_muted,
                getattr(m, "confidence", None), getattr(m, "subtitle", ""),
                getattr(m, "subtitle_state", ""))
    rows = [row(m, int(m.needs_review), 1) for m in muted]
    rows += [row(m, 1, 0) for m in left_in]
    conn.executemany(
        "INSERT INTO detection (job_id, start, end, text, category, review, reason, muted,"
        " confidence, subtitle, subtitle_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def detection(detection_id: int) -> sqlite3.Row | None:
    return connect().execute(
        "SELECT * FROM detection WHERE id=?", (detection_id,)).fetchone()


def detections(job_id: int) -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM detection WHERE job_id=? ORDER BY start", (job_id,)).fetchall()


def cancel_queued(job_id: int) -> bool:
    conn = connect()
    cur = conn.execute(
        "UPDATE job SET status='cancelled', message='cancelled' "
        "WHERE id=? AND status='queued'", (job_id,))
    conn.commit()
    return cur.rowcount > 0


def cleaned_paths(paths: list[str]) -> dict[str, dict]:
    """What this service has already done with each of these files.

    Carries more than the status because the page has to be able to say "this
    was cleaned on Tuesday, 32 words muted - do it again?" rather than silently
    redoing an hour of work.
    """
    if not paths:
        return {}
    out: dict[str, dict] = {}
    conn = connect()
    for chunk in (paths[i:i + 400] for i in range(0, len(paths), 400)):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            # added_bytes is read below, so it has to be selected here. Leaving
            # it out failed only when a path actually had a job - which meant
            # every page looked fine until you opened a show you had cleaned.
            f"SELECT id, path, status, muted, finished_at, added_bytes, action FROM job "
            f"WHERE path IN ({marks}) "
            "AND status IN ('done','skipped','running','queued','failed') "
            "ORDER BY id", chunk).fetchall()
        for row in rows:
            if _removal(row):
                if row["status"] in ("done", "skipped"):
                    out.pop(row["path"], None)
                continue
            out[row["path"]] = {"job_id": row["id"], "status": row["status"],
                                "muted": row["muted"], "finished_at": row["finished_at"],
                                "added_bytes": row["added_bytes"]}
    return out


def _removal(row) -> bool:
    """A job that takes the cleaned track back out. Once it has finished the
    file is not cleaned, whatever came before: the removal job itself is not
    a clean, and must not read as one - it did, and a file whose track had
    been removed was offered "Clean again", and a second Remove found nothing
    to take out. Until it finishes, or if it failed, the track is still there,
    so the clean before it still stands."""
    return (row["action"] or "clean") == "remove"


# The newest finished clean of each file. Cleaning again replaces the track in
# the file, so older jobs for the same path describe a track that is gone -
# listing or summing them showed one file twice and counted its size twice.
_LATEST_CLEAN = ("SELECT MAX(id) FROM job WHERE status IN ('done','skipped') "
                 "AND COALESCE(action,'clean')='clean' GROUP BY path")


def history(query: str = "", limit: int = 500) -> list[sqlite3.Row]:
    """Files that have a cleaned track right now, newest first.

    Removals are not listed: once a track is taken back out, the file is not
    cleaned any more, and its old entry is deleted rather than left to claim
    otherwise.
    """
    # How many of each file's detections are worth a listen - the same rule
    # as subtitles.worth_checking, counted here so the list needs no second query.
    sql = ("SELECT job.*, (SELECT COUNT(*) FROM detection d WHERE d.job_id=job.id"
           " AND (d.subtitle_state IN ('soundalike','subtitle_only')"
           f" OR (COALESCE(d.subtitle_state,'')='' AND d.confidence < {LOW_CONFIDENCE})))"
           " AS to_check"
           f" FROM job WHERE id IN ({_LATEST_CLEAN})")
    args: list = []
    if query:
        sql += " AND (title LIKE ? OR subtitle LIKE ?)"
        args += [f"%{query}%", f"%{query}%"]
    sql += " ORDER BY finished_at DESC, id DESC LIMIT ?"
    args.append(limit)
    return connect().execute(sql, args).fetchall()


def monitor_add(source: str, source_id: str, title: str = "",
                mode: str = "catch_up", keep_existing_mode: bool = False) -> None:
    """`keep_existing_mode` is for marking a show as a side effect of cleaning
    something: it must not quietly downgrade a show the household explicitly
    set to catch up."""
    conn = connect()
    update = ("title=excluded.title" if keep_existing_mode
              else "title=excluded.title, mode=excluded.mode")
    conn.execute(
        "INSERT INTO monitor (source, source_id, title, added_at, mode) VALUES (?,?,?,?,?) "
        f"ON CONFLICT(source, source_id) DO UPDATE SET {update}",
        (source, str(source_id), title, time.time(), mode))
    conn.commit()


def monitor_remove(source: str, source_id: str) -> None:
    conn = connect()
    conn.execute("DELETE FROM monitor WHERE source=? AND source_id=?",
                 (source, str(source_id)))
    if source == "films":
        conn.execute("DELETE FROM film_seen")
    conn.commit()


def films_seen() -> set[str]:
    return {r["path"] for r in connect().execute("SELECT path FROM film_seen")}


def films_mark_seen(paths) -> None:
    conn = connect()
    conn.executemany("INSERT OR IGNORE INTO film_seen (path) VALUES (?)",
                     [(str(p),) for p in paths])
    conn.commit()


def monitors(source: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM monitor"
    args: list = []
    if source:
        sql += " WHERE source=?"
        args.append(source)
    return connect().execute(sql + " ORDER BY title", args).fetchall()


def is_monitored(source: str, source_id: str) -> bool:
    return connect().execute(
        "SELECT 1 FROM monitor WHERE source=? AND source_id=?",
        (source, str(source_id))).fetchone() is not None


def monitor_touch(source: str, source_id: str) -> None:
    conn = connect()
    conn.execute("UPDATE monitor SET last_checked=? WHERE source=? AND source_id=?",
                 (time.time(), source, str(source_id)))
    conn.commit()


def job_paths() -> dict[str, str]:
    """Every path this service has a job for, and its latest status. Used to
    mark up a library listing without one query per episode."""
    rows = connect().execute(
        "SELECT path, status, action FROM job ORDER BY id").fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if _removal(r):
            if r["status"] in ("done", "skipped"):
                out.pop(r["path"], None)
            continue
        out[r["path"]] = r["status"]
    return out


def job_titles() -> dict[str, list[tuple[str, str]]]:
    """{show title: [(path, latest status)]}, for a library that reports no
    show folder to match on."""
    rows = connect().execute(
        "SELECT title, path, status FROM job WHERE kind='episode' "
        "AND COALESCE(action,'clean')='clean' ORDER BY id").fetchall()
    latest: dict[str, tuple[str, str]] = {}
    for r in rows:
        latest[r["path"]] = (r["title"], r["status"])
    out: dict[str, list[tuple[str, str]]] = {}
    for path, (title, status) in latest.items():
        out.setdefault(title, []).append((path, status))
    return out


def judge_lookup(key: str) -> tuple[str, str] | None:
    row = connect().execute(
        "SELECT verdict, instead FROM judge_cache WHERE key=?", (key,)).fetchone()
    return (row["verdict"], row["instead"]) if row else None


def judge_remember(key: str, verdict: str, instead: str = "", word: str = "") -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO judge_cache (key, verdict, instead, word, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET verdict=excluded.verdict,"
        " instead=excluded.instead, word=excluded.word",
        (key, verdict, instead, word, time.time()))
    conn.commit()


def cleaned_before(path: str, job_id: int = 0) -> bool:
    """Has a job other than this one already cleaned this file?

    The record that a track in the file is ours even when the file itself
    carries no proof - which is every file cleaned before the marker was
    written into it.
    """
    row = connect().execute(
        "SELECT 1 FROM job WHERE path=? AND id<>? AND status IN ('done','skipped') "
        "AND COALESCE(action,'clean')='clean' LIMIT 1", (path, job_id)).fetchone()
    return row is not None


def forget_cleaned(path: str) -> int:
    """Drop the record of a file having been cleaned, after its track is removed."""
    conn = connect()
    cur = conn.execute(
        "DELETE FROM job WHERE path=? AND status IN ('done','skipped') "
        "AND COALESCE(action,'clean')='clean'", (path,))
    conn.commit()
    return cur.rowcount


def stats() -> dict:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS jobs, COALESCE(SUM(muted),0) AS muted,"
        " COALESCE(SUM(added_bytes),0) AS bytes FROM job "
        f"WHERE status='done' AND id IN ({_LATEST_CLEAN})").fetchone()
    # Counted rather than derived from the page of jobs the UI asked for - with
    # a few hundred queued, "100" would be the page size, not the truth.
    waiting = conn.execute(
        "SELECT COUNT(*) AS n FROM job WHERE status IN ('queued','running')").fetchone()
    return {"cleaned_files": row["jobs"], "words_muted": row["muted"],
            "waiting": waiting["n"], "added_bytes": row["bytes"]}


def purge(statuses: tuple[str, ...] = ("cancelled",)) -> int:
    """Delete finished job rows outright.

    Only for records nobody wants to keep - a cancelled job is a decision, not
    a result. Detections go with them through the foreign key.
    """
    if not statuses:
        return 0
    conn = connect()
    marks = ",".join("?" * len(statuses))
    cur = conn.execute(f"DELETE FROM job WHERE status IN ({marks})", statuses)
    conn.commit()
    return cur.rowcount


def count_by_status() -> dict[str, int]:
    return {r["status"]: r["n"] for r in connect().execute(
        "SELECT status, COUNT(*) AS n FROM job GROUP BY status")}


def cancel_all_queued() -> int:
    """Empty the queue, leaving whatever is running to finish."""
    conn = connect()
    cur = conn.execute(
        "UPDATE job SET status='cancelled', message='cancelled with the rest of the queue' "
        "WHERE status='queued'")
    conn.commit()
    return cur.rowcount
