"""SQLite storage: sources, jobs and run records."""

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.models import Job, MatchResult, Profile, Source

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

CREATE TABLE IF NOT EXISTS profiles (
    profile_id      TEXT PRIMARY KEY,
    added_at        TEXT NOT NULL        -- their first list only covers jobs first seen up to 7 days before this
);

-- One row per job the AI scored for a person. Jobs that fail the rules get no row and are re-checked each run,
-- so profile edits apply to recent jobs. Delivery and decision are separate on purpose.
CREATE TABLE IF NOT EXISTS matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,   -- short ID for Telegram buttons
    job_key         TEXT NOT NULL REFERENCES jobs(job_key),
    profile_id      TEXT NOT NULL,
    fit             TEXT NOT NULL,       -- strong | good | weak | no (never sent)
    matched_skills  TEXT,                -- JSON list
    missing_skills  TEXT,                -- JSON list
    red_flags       TEXT,                -- JSON list
    reason          TEXT,
    rubric_version  TEXT NOT NULL,
    run_id          TEXT,
    scored_at       TEXT NOT NULL,
    delivery        TEXT NOT NULL DEFAULT 'pending',     -- pending | sent | failed | overflow
    sent_at         TEXT,
    telegram_message_id INTEGER,
    decision        TEXT NOT NULL DEFAULT 'none',        -- none | applied | skipped
    decided_at      TEXT,
    UNIQUE (job_key, profile_id)
);
CREATE INDEX IF NOT EXISTS matches_delivery ON matches(profile_id, delivery);

CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    profile_id      TEXT,
    at              TEXT NOT NULL,
    jobs            INTEGER NOT NULL,
    scored          INTEGER NOT NULL,
    seconds         REAL NOT NULL,
    error_reason    TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or os.environ.get("JOBFINDER_DB") or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)  # the hourly tap collector may write while a run is going
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


# ---------------------------------------------------------------- jobs for matching

def job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        source=row["source"], ats_id=row["ats_id"] or "", title=row["title"], company=row["company"],
        location_raw=row["location_raw"] or "", cities=json.loads(row["cities"] or "[]"),
        work_mode=row["work_mode"] or "unknown", india_eligible=row["india_eligible"] or "unclear",
        experience_min=row["experience_min"], experience_max=row["experience_max"], salary=row["salary"],
        department=row["department"], description=row["description"] or "", apply_url=row["apply_url"],
        posted_at=row["posted_at"], updated_at=row["updated_at"],
    )


def sync_profiles(conn, profiles: list[Profile]) -> None:
    """Remember when each person was first seen, for their first-list window."""
    with conn:
        for p in profiles:
            conn.execute("INSERT OR IGNORE INTO profiles (profile_id, added_at) VALUES (?, ?)", (p.id, now()))


def unscored_jobs(conn, profile: Profile) -> list[tuple[str, Job]]:
    """Open, non-backfill jobs this person has no match for yet, first seen recently enough to matter."""
    added = conn.execute("SELECT added_at FROM profiles WHERE profile_id = ?", (profile.id,)).fetchone()[0]
    newest_window = datetime.now(timezone.utc) - timedelta(days=profile.max_age_days)
    first_list = datetime.fromisoformat(added) - timedelta(days=BACKFILL_DAYS)
    since = max(newest_window, first_list).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT j.* FROM jobs j
           WHERE j.backfill = 0 AND j.lifecycle = 'open' AND j.india_eligible != 'no' AND j.first_seen >= ?
             AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.job_key = j.job_key AND m.profile_id = ?)
           ORDER BY j.first_seen""",
        (since, profile.id),
    ).fetchall()
    return [(r["job_key"], job_from_row(r)) for r in rows]


def save_match(conn, run_id: str | None, profile_id: str, job_key: str, result: MatchResult, rubric: str) -> None:
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO matches (job_key, profile_id, fit, matched_skills, missing_skills, red_flags,
                   reason, rubric_version, run_id, scored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (job_key, profile_id, result.fit, json.dumps(result.matched_skills), json.dumps(result.missing_skills),
             json.dumps(result.red_flags), result.reason, rubric, run_id, now()),
        )


def record_llm_call(conn, run_id, profile_id, jobs: int, scored: int, seconds: float, error: str | None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO llm_calls (run_id, profile_id, at, jobs, scored, seconds, error_reason)
               VALUES (?,?,?,?,?,?,?)""",
            (run_id, profile_id, now(), jobs, scored, round(seconds, 1), error),
        )


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
