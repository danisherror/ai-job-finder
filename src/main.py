"""Entry point. Usage: uv run python -m src.main collect"""

import fcntl
import sys
import time
import traceback
from pathlib import Path

from src import store
from src.collect import ADAPTERS
from src.collect.http import SourceError
from src.config import ROOT, load_sources

LOCK_FILE = ROOT / "private" / "run.lock"
DELAY_BETWEEN_SOURCES = 1.0  # seconds; be polite


def acquire_lock():
    """Exit immediately if another run holds the lock. The lock is released when the process ends."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")
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


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else "collect"
    lock = acquire_lock()  # noqa: F841  (kept open for the life of the process)
    if command == "collect":
        collect()
    else:
        sys.exit(f"Unknown command: {command}")


if __name__ == "__main__":
    main(sys.argv)
