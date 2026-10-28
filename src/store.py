"""SQLite storage: sources, jobs and run records."""

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.models import AGGREGATOR_TYPES, Job, MatchResult, Profile, Source

DEFAULT_DB = Path(__file__).resolve().parent.parent / "private" / "jobs.db"
BACKFILL_DAYS = 7  # on a source's first scan, older jobs are stored as already seen
CLOSE_AFTER_MISSES = 2     # successful scans without a job before it counts as closed
AGGREGATOR_CLOSE_DAYS = 30  # job-board jobs close by age (a board scan only reads its newest pages)
RESUME_WITHIN_HOURS = 6    # an unfinished run younger than this is resumed; older ones are marked failed
SUSPICIOUS_MIN_USUAL = 3   # a source that usually has at least this many jobs and suddenly has 0 is suspicious
ACCEPT_ZERO_AFTER = 3      # ...unless it has had 0 this many runs in a row; then the zero is believed
USUAL_FROM_LAST = 7        # usual job count = median of the last N successful scans
KEEP_LOGS_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name            TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    board           TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    usual_count     INTEGER,             -- typical job count, for suspicious-zero checks
    first_scan_at   TEXT,                -- set after the first successful scan
    last_scanned_at TEXT                 -- last successful scan; `collect` skips sources scanned recently
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
    state           TEXT NOT NULL,       -- running | done | failed (failed = no source scanned, or crashed)
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

-- Everyone who pressed Start in the bot. They all get the same daily internship and 2027-graduate lists.
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id         INTEGER PRIMARY KEY,
    name            TEXT,
    username        TEXT,
    joined_at       TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1   -- 0 after /stop or when they block the bot
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
    _add_last_scanned_at(conn)
    return conn


def _add_last_scanned_at(conn) -> None:
    """Older databases lack sources.last_scanned_at: add it, filled from each source's last successful scan."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    if "last_scanned_at" in columns:
        return
    with conn:
        conn.execute("ALTER TABLE sources ADD COLUMN last_scanned_at TEXT")
        conn.execute("""UPDATE sources SET last_scanned_at =
                            (SELECT MAX(finished) FROM source_runs WHERE source = sources.name AND result = 'ok')""")


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


def resume_or_start_run(conn: sqlite3.Connection) -> tuple[str, bool]:
    """Continue a recent run that never finished (the Mac slept or the process was killed), else start one.

    Only one run can hold the run lock, so any run still marked `running` here was interrupted.
    Returns (run_id, resumed).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RESUME_WITHIN_HOURS)).isoformat(timespec="seconds")
    unfinished = conn.execute("SELECT run_id, started FROM runs WHERE state = 'running' ORDER BY started DESC"
                              ).fetchall()
    resume = unfinished[0]["run_id"] if unfinished and unfinished[0]["started"] >= cutoff else None
    with conn:
        for r in unfinished:
            if r["run_id"] != resume:
                conn.execute("UPDATE runs SET state = 'failed', finished = ? WHERE run_id = ?", (now(), r["run_id"]))
    return (resume, True) if resume else (start_run(conn), False)


def done_sources(conn, run_id: str) -> set[str]:
    """Sources this run has already finished (ok or suspicious). Failed ones are tried again on resume."""
    rows = conn.execute("SELECT source FROM source_runs WHERE run_id = ? AND result IN ('ok', 'suspicious')",
                        (run_id,)).fetchall()
    return {r[0] for r in rows}


def recently_scanned(conn, days: float) -> set[str]:
    """Sources whose last successful scan was less than `days` days ago."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT name FROM sources WHERE last_scanned_at >= ?", (cutoff,)).fetchall()
    return {r[0] for r in rows}


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


def usual_count(conn, source_name: str) -> int | None:
    """Median job count over the source's last successful scans; None before its first one."""
    rows = conn.execute(
        "SELECT jobs_found FROM source_runs WHERE source = ? AND result = 'ok' ORDER BY started DESC, rowid DESC LIMIT ?",
        (source_name, USUAL_FROM_LAST),
    ).fetchall()
    counts = sorted(r[0] for r in rows)
    return counts[len(counts) // 2] if counts else None


def is_suspicious_zero(conn, source: Source, jobs: list[Job]) -> bool:
    """True when a source that usually has jobs suddenly returns none (a broken feed, not a hiring freeze).

    After ACCEPT_ZERO_AFTER suspicious zeros in a row the zero is believed, so its jobs can finally close.
    """
    if jobs:
        return False
    usual = usual_count(conn, source.name)
    if usual is None or usual < SUSPICIOUS_MIN_USUAL:
        return False
    recent = conn.execute("SELECT result FROM source_runs WHERE source = ? ORDER BY started DESC, rowid DESC LIMIT ?",
                          (source.name, ACCEPT_ZERO_AFTER - 1)).fetchall()
    in_a_row = len(recent) == ACCEPT_ZERO_AFTER - 1 and all(r[0] == "suspicious" for r in recent)
    return not in_a_row


def record_suspicious(conn, run_id: str, source: Source, started: str) -> None:
    """A suspicious zero: logged, but no job is touched and it doesn't count as a successful scan."""
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO source_runs
               (run_id, source, result, started, finished, jobs_found, jobs_new, error_reason, error_detail)
               VALUES (?, ?, 'suspicious', ?, ?, 0, 0, 'zero_jobs', ?)""",
            (run_id, source.name, started, now(), f"0 jobs; usually {usual_count(conn, source.name)}"),
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


def known_ats_ids(conn, source_name: str) -> frozenset[str]:
    rows = conn.execute("SELECT ats_id FROM jobs WHERE source = ? AND ats_id IS NOT NULL", (source_name,))
    return frozenset(r[0] for r in rows)


_COMPANY_FILLER = {"inc", "ltd", "llc", "pvt", "private", "limited", "corp", "corporation", "co", "the",
                   "technologies", "technology", "labs", "gmbh", "ai", "io", "hq", "group"}


def same_job_key(company: str, title: str) -> tuple[str, str]:
    """Loose (company, title) identity, used to spot one job listed on a job board and a company feed."""
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in _COMPANY_FILLER]
    return "".join(words), " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def _open_jobs_by_identity(conn, aggregators: bool) -> dict[tuple[str, str], list[str]]:
    """Open jobs from job boards (aggregators=True) or from company feeds, by same_job_key."""
    marks = ",".join("?" * len(AGGREGATOR_TYPES))
    rows = conn.execute(
        f"""SELECT j.job_key, j.company, j.title FROM jobs j JOIN sources s ON s.name = j.source
            WHERE j.lifecycle != 'closed' AND s.type {'' if aggregators else 'NOT'} IN ({marks})""",
        tuple(AGGREGATOR_TYPES),
    )
    index: dict[tuple[str, str], list[str]] = {}
    for key, company, title in rows:
        index.setdefault(same_job_key(company, title), []).append(key)
    return index


def save_source_result(conn, run_id: str, source: Source, started: str, jobs: list[Job]) -> int:
    """Save one source's jobs and mark it done, in a single transaction. Returns the number of new jobs.

    Call this only for a successful scan: jobs of this source that were not in it move towards closed
    (possibly_closed after one miss, closed after CLOSE_AFTER_MISSES). A job that shows up again reopens.

    Job boards (aggregators) differ: a job already open from a company feed is skipped, and their jobs
    close by age instead of by misses. When a company feed adds a job a board already had, the board's
    copy closes, so each job is matched and sent once.
    """
    ts = now()
    first_scan = conn.execute(
        "SELECT first_scan_at IS NULL FROM sources WHERE name = ?", (source.name,)
    ).fetchone()[0]
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    new = 0
    seen: list[str] = []
    others = _open_jobs_by_identity(conn, aggregators=not source.aggregator)
    with conn:  # one transaction: either all of this happens or none of it
        for job in jobs:
            key = job_key(source.type, source.board, job)
            seen.append(key)
            copies = others.get(same_job_key(job.company, job.title), [])
            if source.aggregator and copies:
                continue  # the company's own feed already has it
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
                for copy in copies:  # a job board listed this first; keep the company's own posting
                    conn.execute("UPDATE jobs SET lifecycle = 'closed' WHERE job_key = ?", (copy,))
            elif not job.description:  # known, and its details weren't fetched again: keep what's stored
                conn.execute(  # sitemap readers don't know a known job's title: keep the saved one
                    """UPDATE jobs SET last_seen = ?, missed_scans = 0, title = COALESCE(NULLIF(?, ''), title),
                           lifecycle = 'open' WHERE job_key = ?""",
                    (ts, job.title, key),
                )
            else:  # already known: only refresh what changes over time
                conn.execute(
                    """UPDATE jobs SET last_seen = ?, missed_scans = 0, updated_at = ?,
                           title = ?, description = ?, location_raw = ?, cities = ?, work_mode = ?,
                           india_eligible = ?, experience_min = ?, experience_max = ?, salary = ?,
                           lifecycle = 'open'
                       WHERE job_key = ?""",
                    (ts, job.updated_at.isoformat() if job.updated_at else None, job.title, job.description,
                     job.location_raw, json.dumps(job.cities), job.work_mode, job.india_eligible,
                     job.experience_min, job.experience_max, job.salary, key),
                )
        if source.aggregator:
            old = (datetime.now(timezone.utc) - timedelta(days=AGGREGATOR_CLOSE_DAYS)).isoformat()
            conn.execute(
                """UPDATE jobs SET lifecycle = 'closed'
                   WHERE source = ? AND lifecycle != 'closed' AND COALESCE(posted_at, first_seen) < ?""",
                (source.name, old),
            )
        else:
            # This source's jobs that weren't in this scan were missed once more.
            conn.execute(
                """UPDATE jobs SET missed_scans = missed_scans + 1,
                       lifecycle = CASE WHEN missed_scans + 1 >= ? THEN 'closed' ELSE 'possibly_closed' END
                   WHERE source = ? AND lifecycle != 'closed'
                     AND job_key NOT IN (SELECT value FROM json_each(?))""",
                (CLOSE_AFTER_MISSES, source.name, json.dumps(seen)),
            )
        conn.execute(
            """INSERT OR REPLACE INTO source_runs (run_id, source, result, started, finished, jobs_found, jobs_new)
               VALUES (?, ?, 'ok', ?, ?, ?, ?)""",
            (run_id, source.name, started, ts, len(jobs), new),
        )
        conn.execute(
            "UPDATE sources SET first_scan_at = COALESCE(first_scan_at, ?), last_scanned_at = ? WHERE name = ?",
            (ts, ts, source.name),
        )
    with conn:
        conn.execute("UPDATE sources SET usual_count = ? WHERE name = ?", (usual_count(conn, source.name), source.name))
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


# ---------------------------------------------------------------- subscribers

def subscribe(conn, chat_id: int, name: str | None, username: str | None) -> bool:
    """Add or reactivate a subscriber. Returns True if they were not already active."""
    row = conn.execute("SELECT active FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    with conn:
        conn.execute(
            """INSERT INTO subscribers (chat_id, name, username, joined_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET active = 1, name = excluded.name, username = excluded.username""",
            (chat_id, name, username, now()),
        )
    return not (row and row["active"])


def unsubscribe(conn, chat_id: int) -> None:
    with conn:
        conn.execute("UPDATE subscribers SET active = 0 WHERE chat_id = ?", (chat_id,))


def active_subscribers(conn) -> list[int]:
    return [r[0] for r in conn.execute("SELECT chat_id FROM subscribers WHERE active = 1 ORDER BY joined_at")]


# ---------------------------------------------------------------- retention

def delete_old_logs(conn, days: int = KEEP_LOGS_DAYS) -> dict:
    """Delete run records and AI call logs older than `days`. Jobs and matches are kept."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with conn:
        old_runs = "SELECT run_id FROM runs WHERE started < ? AND state != 'running'"
        source_runs = conn.execute(f"DELETE FROM source_runs WHERE run_id IN ({old_runs})", (cutoff,)).rowcount
        runs = conn.execute(f"DELETE FROM runs WHERE run_id IN ({old_runs})", (cutoff,)).rowcount
        calls = conn.execute("DELETE FROM llm_calls WHERE at < ?", (cutoff,)).rowcount
    return {"runs": runs, "source_runs": source_runs, "llm_calls": calls}


def forget_profile(conn, profile_id: str) -> dict:
    """Delete everything stored about one person. Jobs and run logs are shared, so they stay."""
    with conn:
        removed = {
            "matches": conn.execute("DELETE FROM matches WHERE profile_id = ?", (profile_id,)).rowcount,
            "ai calls": conn.execute("DELETE FROM llm_calls WHERE profile_id = ?", (profile_id,)).rowcount,
            "state": conn.execute("DELETE FROM state WHERE key IN (?, ?)",
                                  (f"digest_day:{profile_id}", f"alert:send_{profile_id}")).rowcount,
            "profile": conn.execute("DELETE FROM profiles WHERE profile_id = ?", (profile_id,)).rowcount,
        }
    return removed
