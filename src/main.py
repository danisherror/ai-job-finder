"""Entry point.

    uv run python -m src.main daily              collect, match, send lists (the 6:45 job)
    uv run python -m src.main collect            fetch all sources into the job pool (also writes all_jobs.csv)
    uv run python -m src.main export             write every collected job to private/exports/all_jobs.csv
    uv run python -m src.main match              score new jobs for each person
    uv run python -m src.main digest [--dry-run] send (or just print) each person's list
    uv run python -m src.main taps               save Applied/Skipped button taps (the hourly job)
    uv run python -m src.main health [--send]    print (or send to the owner) the weekly health summary
    uv run python -m src.main forget <person>    delete everything stored about someone (remove their profile first)
"""

import fcntl
import sys
import time
import traceback
from pathlib import Path

from src import digest, health, matcher, store, telegram
from src.collect import ADAPTERS
from src.collect.http import SourceError
from src.config import ROOT, load_env, load_profiles, load_sources

LOCK_FILE = ROOT / "private" / "run.lock"
TAPS_LOCK_FILE = ROOT / "private" / "taps.lock"  # separate, so the hourly job never blocks the daily run
LOGS_DIR = ROOT / "private" / "logs"
MAX_LOG_BYTES = 2_000_000
DELAY_BETWEEN_SOURCES = 1.0  # seconds; be polite


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


def collect(conn, run_id: str) -> tuple[int, int]:
    """Stage 1: fetch every active source into the job pool. Returns (sources scanned, active sources).

    On a resumed run, sources this run already finished are skipped.
    """
    sources = [s for s in load_sources() if s.active]
    store.sync_sources(conn, sources)
    done = store.done_sources(conn, run_id)
    todo = [s for s in sources if s.name not in done]
    print(f"Run {run_id}: collecting from {len(todo)} sources" + (f" ({len(done)} already done)" if done else ""))

    for i, source in enumerate(todo):
        if i:
            time.sleep(DELAY_BETWEEN_SOURCES)
        started = store.now()
        try:
            jobs = ADAPTERS[source.type](source, store.known_ats_ids(conn, source.name))
            if store.is_suspicious_zero(conn, source, jobs):
                store.record_suspicious(conn, run_id, source, started)
                print(f"  SUSPECT {source.name:<14}    0 jobs (usually {store.usual_count(conn, source.name)}); "
                      "nothing closed")
                continue
            new = store.save_source_result(conn, run_id, source, started, jobs)
            print(f"  ok      {source.name:<14} {len(jobs):>4} jobs, {new:>4} new")
        except SourceError as e:
            store.record_source_failure(conn, run_id, source, started, e.reason, str(e))
            print(f"  FAILED  {source.name:<14} {e.reason}: {e}")
        except Exception as e:  # a bug or an unexpected feed shape: record it, keep going
            store.record_source_failure(conn, run_id, source, started, "error", traceback.format_exc())
            print(f"  FAILED  {source.name:<14} error: {e!r}")

    scanned = conn.execute("SELECT COUNT(*) FROM source_runs WHERE run_id = ? AND result = 'ok'",
                           (run_id,)).fetchone()[0]
    print(f"Scanned {scanned} of {len(sources)} sources")
    export_jobs(conn)
    return scanned, len(sources)


def export_jobs(conn) -> None:
    count = digest.export_all_jobs(conn)
    print(f"Wrote {count} jobs to {digest.ALL_JOBS_CSV.relative_to(ROOT)}")


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
        run_id, _ = store.resume_or_start_run(conn)
        scanned, _ = collect(conn, run_id)
        store.finish_run(conn, run_id, "done" if scanned else "failed")
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
