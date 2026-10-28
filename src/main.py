"""Entry point.

    uv run python -m src.main daily              collect, match, send lists (the 6:45 job)
    uv run python -m src.main collect [--days N] [--all]
                                                 fetch sources into the job pool (also writes the CSV exports);
                                                 skips sources scanned in the last N days (default 1), --all scans every one
    uv run python -m src.main export             write the CSV exports (all jobs; software internships and 2027-graduate jobs in India)
    uv run python -m src.main match              score new jobs for each person
    uv run python -m src.main digest [--dry-run] send (or just print) each person's list
    uv run python -m src.main taps               save Applied/Skipped button taps (the hourly job)
    uv run python -m src.main health [--send]    print (or send to the owner) the weekly health summary
    uv run python -m src.main forget <person>    delete everything stored about someone (remove their profile first)
"""

import fcntl
import queue
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src import digest, health, matcher, store, telegram
from src.collect import ADAPTERS
from src.collect.http import SourceError
from src.config import ROOT, load_env, load_profiles, load_sources
from src.models import Source

LOCK_FILE = ROOT / "private" / "run.lock"
TAPS_LOCK_FILE = ROOT / "private" / "taps.lock"  # separate, so the hourly job never blocks the daily run
LOGS_DIR = ROOT / "private" / "logs"
MAX_LOG_BYTES = 2_000_000
DELAY_BETWEEN_SOURCES = 1.0  # seconds between sources of one lane; be polite
MAX_LANES = 8                # source types scanned at the same time
RESCAN_AFTER_DAYS = 1.0      # `collect` skips sources successfully scanned more recently than this


def acquire_lock(path: Path = LOCK_FILE):
    """Exit immediately if another run holds the lock. The lock is released when the process ends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another run is in progress; exiting.")
        sys.exit(0)
    return handle


def collect(conn, run_id: str, skip_within_days: float | None = None) -> tuple[int, int]:
    """Stage 1: fetch every active source into the job pool. Returns (sources scanned, active sources).

    Sources run in lanes, one per source type (Greenhouse, Workday, Eightfold, ...), up to
    MAX_LANES at a time. Within a lane sources run one after another, because companies on the
    same system share its servers (Eightfold rate-limited us when scanned back to back). Threads
    only download; all database work stays on this thread, in the order sources finish.

    On a resumed run, sources this run already finished are skipped. With skip_within_days, sources
    whose last successful scan is more recent than that are skipped too (failed ones are always retried).
    """
    run_clock = time.monotonic()
    sources = [s for s in load_sources() if s.active]
    store.sync_sources(conn, sources)
    done = store.done_sources(conn, run_id)
    recent = store.recently_scanned(conn, skip_within_days) - done if skip_within_days else set()
    todo = [s for s in sources if s.name not in done and s.name not in recent]
    notes = ([f"{len(done)} already done"] if done else []) + \
            ([f"{len(recent)} skipped, scanned in the last {skip_within_days:g} day(s); use --all to include them"]
             if recent else [])
    print(f"Run {run_id}: collecting from {len(todo)} sources" + (f" ({'; '.join(notes)})" if notes else ""))

    lanes: dict[str, list[Source]] = {}
    for source in todo:
        lanes.setdefault(source.type, []).append(source)
    known = {s.name: store.known_ats_ids(conn, s.name) for s in todo}
    width = max((len(s.name) for s in todo), default=14)
    results: queue.Queue = queue.Queue()

    def run_lane(lane: list[Source]) -> None:
        for i, source in enumerate(lane):
            if i:
                time.sleep(DELAY_BETWEEN_SOURCES)
            started, clock = store.now(), time.monotonic()
            try:
                jobs, error = ADAPTERS[source.type](source, known[source.name]), None
            except Exception as e:  # handed to the main thread, which records it
                jobs, error = None, e
            results.put((source, started, time.monotonic() - clock, jobs, error))

    with ThreadPoolExecutor(max_workers=min(MAX_LANES, len(lanes)) or 1) as pool:
        for lane in lanes.values():
            pool.submit(run_lane, lane)
        for _ in todo:
            source, started, seconds, jobs, error = results.get()
            save_result(conn, run_id, source, started, jobs, error, seconds, width)

    scanned = conn.execute("SELECT COUNT(*) FROM source_runs WHERE run_id = ? AND result = 'ok'",
                           (run_id,)).fetchone()[0]
    print(f"Scanned {scanned} of {len(sources)} sources in {format_duration(time.monotonic() - run_clock)}")
    export_jobs(conn)
    print(f"Collect finished at {time.strftime('%H:%M')}, took {format_duration(time.monotonic() - run_clock)} in total")
    return scanned, len(sources)


def format_duration(seconds: float) -> str:
    """How long something took, e.g. '8.4s', '2m 05s' or '1h 03m 20s'."""
    if seconds < 59.95:  # would round to "60.0s"
        return f"{seconds:.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def save_result(conn, run_id: str, source: Source, started: str, jobs: list | None, error: Exception | None,
                seconds: float = 0.0, width: int = 14) -> None:
    name, took = f"{source.name:<{width}}", f"{format_duration(seconds):>7}"
    if isinstance(error, SourceError):
        store.record_source_failure(conn, run_id, source, started, error.reason, str(error))
        print(f"  FAILED  {name} {took}  {error.reason}: {error}")
        return
    if error is not None:  # a bug or an unexpected feed shape: record it, keep going
        detail = "".join(traceback.format_exception(error))
        store.record_source_failure(conn, run_id, source, started, "error", detail)
        print(f"  FAILED  {name} {took}  error: {error!r}")
        return
    try:
        if store.is_suspicious_zero(conn, source, jobs):
            store.record_suspicious(conn, run_id, source, started)
            print(f"  SUSPECT {name} {took}      0 jobs (usually {store.usual_count(conn, source.name)}); "
                  "nothing closed")
            return
        new = store.save_source_result(conn, run_id, source, started, jobs)
        print(f"  ok      {name} {took}  {len(jobs):>5} jobs, {new:>5} new")
    except Exception as e:  # e.g. a job the store rejects: record it, keep going
        store.record_source_failure(conn, run_id, source, started, "error", traceback.format_exc())
        print(f"  FAILED  {name} {took}  error: {e!r}")


def export_jobs(conn) -> None:
    count = digest.export_all_jobs(conn)
    print(f"Wrote {count} jobs to private/exports/{digest.ALL_JOBS_CSV}")
    count = digest.export_india_internships(conn)
    print(f"Wrote {count} software internships to private/exports/{digest.INDIA_INTERNSHIPS_CSV}")
    count = digest.export_graduates(conn)
    print(f"Wrote {count} software jobs for {digest.GRAD_YEAR} graduates to private/exports/{digest.GRADUATES_CSV}")


def latest_run_id(conn) -> str | None:
    row = conn.execute("SELECT run_id FROM runs ORDER BY started DESC LIMIT 1").fetchone()
    return row[0] if row else None


def match(conn, run_id: str | None = None) -> dict:
    """Stage 2: rules then AI, per person. Returns each person's counts."""
    profiles = load_profiles()
    store.sync_profiles(conn, profiles)
    if not profiles:
        print("No profiles in config/profiles/ (example.yaml is only a template)")
    results = {}
    for p in profiles:
        t = time.monotonic()
        r = results[p.id] = matcher.match_profile(conn, run_id, p)
        print(f"  {p.id:<12} {r['candidates']} new jobs, {r['passed']} passed rules, {r['scored']} scored, "
              f"{r['unscored']} left for next run ({time.monotonic() - t:.0f}s)")
        if r["dropped"]:
            print("               dropped by rules: " + ", ".join(f"{k} {v}" for k, v in r["dropped"].items()))
        if r["errors"]:
            print(f"               AI errors: {', '.join(r['errors'])}" + (f"; stopped ({r['stopped']})" if r["stopped"] else ""))
    return results


def send(conn, run_id: str | None = None, dry_run: bool = False, once_a_day: bool = False) -> dict[str, str]:
    """Stage 3: each person's list. Returns {person: error} for lists that could not be sent."""
    run_id = run_id or latest_run_id(conn)
    failures = {}
    for p in load_profiles():
        try:
            r = digest.send_digest(conn, p, run_id, dry_run=dry_run, once_a_day=once_a_day)
        except telegram.TelegramError as e:
            print(f"  {p.id:<12} FAILED to send: {e}")
            failures[p.id] = str(e)
            continue
        if r.get("skipped"):
            print(f"  {p.id:<12} skipped: {r['skipped']}")
        elif not r["dry_run"]:
            print(f"  {p.id:<12} sent {r['sent']}, failed {r['failed']}, overflow {r['overflow']}")
    return failures


def daily(conn) -> None:
    """The 6:45 job: collect, match, send, then alerts, the weekly summary and cleanup."""
    run_id, resumed = store.resume_or_start_run(conn)
    if resumed:
        print(f"Resuming unfinished run {run_id}")
    try:
        scanned, total = collect(conn, run_id)
        match_results = match(conn, run_id)
        send_failures = send(conn, run_id, once_a_day=True)
    except Exception:
        store.finish_run(conn, run_id, "failed")
        detail = traceback.format_exc()
        print(detail)
        health.alert(conn, "crash", "The daily run crashed:\n<pre>" + health.e(detail[-1500:]) + "</pre>")
        raise
    store.finish_run(conn, run_id, "done" if scanned else "failed")
    health.check_run(conn, run_id, scanned, total, match_results, send_failures)
    health.weekly_if_due(conn, load_profiles())
    cleanup(conn)


def cleanup(conn) -> None:
    """Delete run logs older than KEEP_LOGS_DAYS and keep the log files from growing forever."""
    removed = store.delete_old_logs(conn)
    if any(removed.values()):
        print("Deleted old records: " + ", ".join(f"{k} {v}" for k, v in removed.items()))
    for log in LOGS_DIR.glob("*.log"):
        if log.stat().st_size > MAX_LOG_BYTES:
            tail = log.read_bytes()[-MAX_LOG_BYTES // 2:]
            log.write_bytes(b"[older lines removed]\n" + tail[tail.find(b"\n") + 1:])


def taps() -> None:
    """The hourly job: save button taps, and alert if the daily run has stopped happening."""
    conn = store.connect()
    health.check_stale(conn)
    try:
        counts = telegram.poll_updates(conn, load_profiles())
    except telegram.TelegramError as e:
        sys.exit(f"Taps failed: {e}")
    print(f"Taps saved: {counts['taps']}, ignored: {counts['ignored']}, /start replies: {counts['messages']}")


def forget(conn, person: str) -> None:
    """Delete a person's matches, AI call log, list export, labels and cached AI results."""
    from src import evaluate  # only needed here

    if (ROOT / "config" / "profiles" / f"{person}.yaml").exists():
        sys.exit(f"Delete config/profiles/{person}.yaml (and their resume) first, or the next run adds them back.")
    removed = store.forget_profile(conn, person)
    files = [digest.EXPORTS / f"{person}.csv", evaluate.labels_path(person), evaluate.jobs_path(person),
             evaluate.CACHE_DIR / f"{person}.json"]
    deleted = [f for f in files if f.exists()]
    for f in deleted:
        f.unlink()
    print(f"Deleted for {person}: " + ", ".join(f"{k} {v}" for k, v in removed.items())
          + (f"; files: {', '.join(str(f.relative_to(ROOT)) for f in deleted)}" if deleted else "; no files"))


def option(argv: list[str], name: str, default):
    """The value after `name` in argv (e.g. --days 3), else default."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
        sys.exit(f"{name} needs a value")
    return default


def main(argv: list[str]) -> None:
    load_env()
    command = argv[1] if len(argv) > 1 else "daily"
    if command == "taps":
        lock = acquire_lock(TAPS_LOCK_FILE)  # noqa: F841
        taps()
        return
    lock = acquire_lock()  # noqa: F841  (kept open for the life of the process)
    conn = store.connect()
    if command == "collect":
        skip_days = None if "--all" in argv else float(option(argv, "--days", RESCAN_AFTER_DAYS))
        run_id, _ = store.resume_or_start_run(conn)
        scanned, _ = collect(conn, run_id, skip_within_days=skip_days)
        tried = conn.execute("SELECT COUNT(*) FROM source_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        store.finish_run(conn, run_id, "failed" if tried and not scanned else "done")
    elif command == "export":
        export_jobs(conn)
    elif command == "match":
        match(conn)
    elif command == "digest":
        send(conn, dry_run="--dry-run" in argv)
    elif command == "daily":
        daily(conn)
    elif command == "health":
        text = health.summary(conn, load_profiles())
        if "--send" in argv:
            health.tell_owner(text)
        else:
            print(text)
    elif command == "forget" and len(argv) > 2:
        forget(conn, argv[2])
    else:
        sys.exit(f"Unknown command: {command}")


if __name__ == "__main__":
    main(sys.argv)
