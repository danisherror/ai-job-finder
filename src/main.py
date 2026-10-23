"""Entry point.

    uv run python -m src.main daily              collect, match, send lists (the 6:45 job)
    uv run python -m src.main collect            fetch all sources into the job pool
    uv run python -m src.main match              score new jobs for each person
    uv run python -m src.main digest [--dry-run] send (or just print) each person's list
    uv run python -m src.main taps               save Applied/Skipped button taps (the hourly job)
"""

import fcntl
import sys
import time
import traceback
from pathlib import Path

from src import digest, matcher, store, telegram
from src.collect import ADAPTERS
from src.collect.http import SourceError
from src.config import ROOT, load_env, load_profiles, load_sources

LOCK_FILE = ROOT / "private" / "run.lock"
TAPS_LOCK_FILE = ROOT / "private" / "taps.lock"  # separate, so the hourly job never blocks the daily run
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


def collect() -> str:
    """Stage 1: fetch every active source into the job pool. Returns the run ID."""
    sources = [s for s in load_sources() if s.active]
    conn = store.connect()
    store.sync_sources(conn, sources)
    run_id = store.start_run(conn)
    print(f"Run {run_id}: collecting from {len(sources)} sources")

    ok = 0
    for i, source in enumerate(sources):
        if i:
            time.sleep(DELAY_BETWEEN_SOURCES)
        started = store.now()
        try:
            jobs = ADAPTERS[source.type](source)
            new = store.save_source_result(conn, run_id, source, started, jobs)
            ok += 1
            print(f"  ok      {source.name:<14} {len(jobs):>4} jobs, {new:>4} new")
        except SourceError as e:
            store.record_source_failure(conn, run_id, source, started, e.reason, str(e))
            print(f"  FAILED  {source.name:<14} {e.reason}: {e}")
        except Exception as e:  # a bug or an unexpected feed shape: record it, keep going
            store.record_source_failure(conn, run_id, source, started, "error", traceback.format_exc())
            print(f"  FAILED  {source.name:<14} error: {e!r}")

    store.finish_run(conn, run_id, "done" if ok else "failed")
    print(f"Scanned {ok} of {len(sources)} sources")
    return run_id


def latest_run_id(conn) -> str | None:
    row = conn.execute("SELECT run_id FROM runs ORDER BY started DESC LIMIT 1").fetchone()
    return row[0] if row else None


def match(run_id: str | None = None) -> None:
    """Stage 2: rules then AI, per person."""
    profiles = load_profiles()
    conn = store.connect()
    store.sync_profiles(conn, profiles)
    if not profiles:
        print("No profiles in config/profiles/ (example.yaml is only a template)")
    for p in profiles:
        t = time.monotonic()
        r = matcher.match_profile(conn, run_id, p)
        print(f"  {p.id:<12} {r['candidates']} new jobs, {r['passed']} passed rules, {r['scored']} scored, "
              f"{r['unscored']} left for next run ({time.monotonic() - t:.0f}s)")
        if r["dropped"]:
            print("               dropped by rules: " + ", ".join(f"{k} {v}" for k, v in r["dropped"].items()))
        if r["errors"]:
            print(f"               AI errors: {', '.join(r['errors'])}" + (f"; stopped ({r['stopped']})" if r["stopped"] else ""))


def send(run_id: str | None = None, dry_run: bool = False) -> None:
    """Stage 3: each person's list."""
    conn = store.connect()
    run_id = run_id or latest_run_id(conn)
    for p in load_profiles():
        try:
            r = digest.send_digest(conn, p, run_id, dry_run=dry_run)
        except telegram.TelegramError as e:
            print(f"  {p.id:<12} FAILED to send: {e}")
            continue
        if not r["dry_run"]:
            print(f"  {p.id:<12} sent {r['sent']}, failed {r['failed']}, overflow {r['overflow']}")


def taps() -> None:
    conn = store.connect()
    try:
        counts = telegram.poll_updates(conn, load_profiles())
    except telegram.TelegramError as e:
        sys.exit(f"Taps failed: {e}")
    print(f"Taps saved: {counts['taps']}, ignored: {counts['ignored']}, /start replies: {counts['messages']}")


def main(argv: list[str]) -> None:
    load_env()
    command = argv[1] if len(argv) > 1 else "daily"
    if command == "taps":
        lock = acquire_lock(TAPS_LOCK_FILE)  # noqa: F841
        taps()
        return
    lock = acquire_lock()  # noqa: F841  (kept open for the life of the process)
    if command == "collect":
        collect()
    elif command == "match":
        match()
    elif command == "digest":
        send(dry_run="--dry-run" in argv)
    elif command == "daily":
        run_id = collect()
        match(run_id)
        send(run_id)
    else:
        sys.exit(f"Unknown command: {command}")


if __name__ == "__main__":
    main(sys.argv)
