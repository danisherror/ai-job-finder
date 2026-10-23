"""SQLite storage: sources, jobs and run records."""

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.models import Job, Source

DEFAULT_DB = Path(__file__).resolve().parent.parent / "private" / "jobs.db"
BACKFILL_DAYS = 7  # on a source's first scan, older jobs are stored as already seen

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name            TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    board           TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    usual_count     INTEGER,             -- typical job count, for suspicious-zero checks
    first_scan_at   TEXT                 -- set after the first successful scan
);

CREATE TABLE IF NOT EXISTS jobs (
    job_key         TEXT PRIMARY KEY,
    source          TEXT NOT NULL REFERENCES sources(name),
    ats_id          TEXT,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location_raw    TEXT,
    cities          TEXT,                -- JSON list
    work_mode       TEXT,
    india_eligible  TEXT,
    experience_min  INTEGER,
    experience_max  INTEGER,
    salary          TEXT,
    department      TEXT,
    description     TEXT,
    apply_url       TEXT NOT NULL,
    posted_at       TEXT,
    updated_at      TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    missed_scans    INTEGER NOT NULL DEFAULT 0,
    lifecycle       TEXT NOT NULL DEFAULT 'open',   -- open | possibly_closed | closed
    backfill        INTEGER NOT NULL DEFAULT 0      -- 1 = existed before tracking began; never scored
);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    state           TEXT NOT NULL,       -- running | done | failed
    started         TEXT NOT NULL,
    finished        TEXT
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    source          TEXT NOT NULL REFERENCES sources(name),
    result          TEXT NOT NULL,       -- ok | failed | suspicious
    started         TEXT NOT NULL,
    finished        TEXT NOT NULL,
    jobs_found      INTEGER,
    jobs_new        INTEGER,
    error_reason    TEXT,
    error_detail    TEXT,
    PRIMARY KEY (run_id, source)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or os.environ.get("JOBFINDER_DB") or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- identity

def canonical_url(url: str) -> str:
    """Lowercase scheme/host, drop query string, fragment and trailing slash."""
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))


def job_key(source_type: str, board: str, job: Job) -> str:
    """ATS job ID, else canonical apply URL, else a hash of company + title + location."""
    if job.ats_id:
        return f"{source_type}:{board}:{job.ats_id}"
    if job.apply_url:
        return "url:" + canonical_url(str(job.apply_url))
    raw = f"{job.company}|{job.title}|{job.location_raw}".lower()
    return "hash:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


# ---------------------------------------------------------------- sources

def sync_sources(conn: sqlite3.Connection, sources: list[Source]) -> None:
    """Make the sources table match the config file. Removed sources become inactive, not deleted."""
    with conn:
        conn.execute("UPDATE sources SET active = 0")
        for s in sources:
            conn.execute(
                """INSERT INTO sources (name, type, board, active) VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET type = excluded.type, board = excluded.board,
                                                   active = excluded.active""",
                (s.name, s.type, s.board, int(s.active)),
            )


# ---------------------------------------------------------------- runs

def start_run(conn: sqlite3.Connection) -> str:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    with conn:
        conn.execute("INSERT INTO runs (run_id, state, started) VALUES (?, 'running', ?)", (run_id, now()))
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, state: str) -> None:
    with conn:
        conn.execute("UPDATE runs SET state = ?, finished = ? WHERE run_id = ?", (state, now(), run_id))


def record_source_failure(conn, run_id: str, source: Source, started: str, reason: str, detail: str) -> None:
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO source_runs
               (run_id, source, result, started, finished, error_reason, error_detail)
               VALUES (?, ?, 'failed', ?, ?, ?, ?)""",
            (run_id, source.name, started, now(), reason, detail[:500]),
        )


def _is_backfill(job: Job, cutoff: datetime) -> bool:
    # Use the posting date. Some boards (Stripe, MongoDB) bump updated_at on every job daily,
    # so updated_at is only a fallback when no posting date exists.
    d = job.posted_at or job.updated_at
    if d is None:
        return True  # no date at all: can't prove it's new, so treat as old on the first scan
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d < cutoff


def save_source_result(conn, run_id: str, source: Source, started: str, jobs: list[Job]) -> int:
    """Save one source's jobs and mark it done, in a single transaction. Returns the number of new jobs."""
    ts = now()
    first_scan = conn.execute(
        "SELECT first_scan_at IS NULL FROM sources WHERE name = ?", (source.name,)
    ).fetchone()[0]
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    new = 0
    with conn:  # one transaction: either all of this happens or none of it
        for job in jobs:
            key = job_key(source.type, source.board, job)
            cur = conn.execute(
                """INSERT INTO jobs (job_key, source, ats_id, title, company, location_raw, cities, work_mode,
                       india_eligible, experience_min, experience_max, salary, department, description,
                       apply_url, posted_at, updated_at, first_seen, last_seen, backfill)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_key) DO NOTHING""",
                (key, source.name, job.ats_id, job.title, job.company, job.location_raw,
                 json.dumps(job.cities), job.work_mode, job.india_eligible, job.experience_min,
                 job.experience_max, job.salary, job.department, job.description, str(job.apply_url),
                 job.posted_at.isoformat() if job.posted_at else None,
                 job.updated_at.isoformat() if job.updated_at else None,
                 ts, ts, int(first_scan and _is_backfill(job, cutoff))),
            )
            if cur.rowcount:
                new += 1
            else:  # already known: only refresh what changes over time
                conn.execute(
                    """UPDATE jobs SET last_seen = ?, missed_scans = 0, updated_at = ?,
                           title = ?, description = ?, location_raw = ?, cities = ?, work_mode = ?,
                           india_eligible = ?, experience_min = ?, experience_max = ?, salary = ?,
                           lifecycle = CASE WHEN lifecycle = 'possibly_closed' THEN 'open' ELSE lifecycle END
                       WHERE job_key = ?""",
                    (ts, job.updated_at.isoformat() if job.updated_at else None, job.title, job.description,
                     job.location_raw, json.dumps(job.cities), job.work_mode, job.india_eligible,
                     job.experience_min, job.experience_max, job.salary, key),
                )
        conn.execute(
            """INSERT OR REPLACE INTO source_runs (run_id, source, result, started, finished, jobs_found, jobs_new)
               VALUES (?, ?, 'ok', ?, ?, ?, ?)""",
            (run_id, source.name, started, ts, len(jobs), new),
        )
        conn.execute(
            "UPDATE sources SET first_scan_at = COALESCE(first_scan_at, ?) WHERE name = ?", (ts, source.name)
        )
    return new
