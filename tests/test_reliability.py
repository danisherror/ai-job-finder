"""Phase 3: resumable runs, job lifecycle, suspicious zeros, alerts, weekly health, retention."""

from datetime import datetime, timedelta, timezone

import pytest

from src import digest, health, main, store, telegram
from src.collect.http import SourceError
from src.models import Job, Profile, Source

A = Source(name="Acme", type="lever", board="acme")
B = Source(name="Bolt", type="greenhouse", board="bolt")


def job(source: Source, ats_id: str) -> Job:
    return Job(source=source.name, ats_id=ats_id, title="Backend Engineer", company=source.name,
               location_raw="Bengaluru", cities=["Bengaluru"], india_eligible="yes",
               apply_url=f"https://jobs.lever.co/{source.board}/{ats_id}",
               posted_at=datetime.now(timezone.utc))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = store.connect(tmp_path / "r.db")
    store.sync_sources(c, [A, B])
    monkeypatch.setattr(main, "load_sources", lambda: [A, B])
    monkeypatch.setattr(main, "load_profiles", lambda: [])
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "LOGS_DIR", tmp_path / "logs")
    return c


@pytest.fixture
def owner(monkeypatch):
    """Captures every message to the owner instead of sending it."""
    sent: list[str] = []
    monkeypatch.setattr(health, "tell_owner", lambda text: sent.append(text) or True)
    return sent


def feeds(monkeypatch, **by_source):
    """Fake adapters: by_source maps a source name to a list of job IDs, or to an exception to raise."""
    def fetch(source, known=frozenset()):
        value = by_source[source.name]
        if isinstance(value, Exception):
            raise value
        return [job(source, i) for i in value]
    monkeypatch.setattr(main, "ADAPTERS", {"lever": fetch, "greenhouse": fetch})


def scan(conn, monkeypatch, **by_source):
    feeds(monkeypatch, **by_source)
    run_id = store.start_run(conn)
    main.collect(conn, run_id)
    store.finish_run(conn, run_id, "done")
    return run_id


def lifecycle(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT ats_id, lifecycle FROM jobs").fetchall())


# ---------------------------------------------------------------- resumable runs

def test_restart_skips_sources_already_done(conn, monkeypatch):
    run_id = store.start_run(conn)  # a run that died after scanning Acme
    store.save_source_result(conn, run_id, A, store.now(), [job(A, "a1")])
    calls = []
    feeds(monkeypatch, Acme=["a1"], Bolt=["b1"])
    fetch = main.ADAPTERS["lever"]
    monkeypatch.setattr(main, "ADAPTERS", {"lever": lambda s, k=frozenset(): calls.append(s.name) or fetch(s),
                                           "greenhouse": lambda s, k=frozenset(): calls.append(s.name) or fetch(s)})
    resumed_id, resumed = store.resume_or_start_run(conn)
    assert (resumed_id, resumed) == (run_id, True)
    assert main.collect(conn, resumed_id) == (2, 2)
    assert calls == ["Bolt"]


def test_failed_sources_are_retried_on_resume(conn, monkeypatch):
    run_id = store.start_run(conn)
    store.record_source_failure(conn, run_id, A, store.now(), "timeout", "")
    assert store.done_sources(conn, run_id) == set()


def test_old_unfinished_run_is_not_resumed(conn):
    old = store.start_run(conn)
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=store.RESUME_WITHIN_HOURS + 1)).isoformat()
    conn.execute("UPDATE runs SET started = ? WHERE run_id = ?", (long_ago, old))
    conn.commit()
    new, resumed = store.resume_or_start_run(conn)
    assert new != old and not resumed
    assert conn.execute("SELECT state FROM runs WHERE run_id = ?", (old,)).fetchone()[0] == "failed"


def test_restarted_daily_run_sends_no_second_list(conn, monkeypatch, tmp_path):
    p = Profile(id="asha", name="Asha", telegram_chat_id=1, target_roles=["x"], seniority="mid", experience_years=3)
    monkeypatch.setattr(digest, "EXPORTS", tmp_path / "exports")
    sent = []
    monkeypatch.setattr(telegram, "send_message", lambda *a, **k: sent.append(a) or 1)
    digest.send_digest(conn, p, None, once_a_day=True)
    r = digest.send_digest(conn, p, None, once_a_day=True)
    assert r["skipped"] and len(sent) == 2  # one header + one footer, from the first call only


# ---------------------------------------------------------------- lifecycle

def test_missing_jobs_close_after_two_successful_scans(conn, monkeypatch):
    scan(conn, monkeypatch, Acme=["a1", "a2"], Bolt=[])
    scan(conn, monkeypatch, Acme=["a1"], Bolt=[])
    assert lifecycle(conn) == {"a1": "open", "a2": "possibly_closed"}
    scan(conn, monkeypatch, Acme=["a1"], Bolt=[])
    assert lifecycle(conn) == {"a1": "open", "a2": "closed"}
    scan(conn, monkeypatch, Acme=["a1", "a2"], Bolt=[])  # re-posted with the same ID
    assert lifecycle(conn) == {"a1": "open", "a2": "open"}


def test_failed_scans_never_close_jobs(conn, monkeypatch):
    scan(conn, monkeypatch, Acme=["a1"], Bolt=["b1"])
    for _ in range(3):
        scan(conn, monkeypatch, Acme=SourceError("timeout"), Bolt=["b1"])
    assert lifecycle(conn) == {"a1": "open", "b1": "open"}


def test_one_source_never_closes_another_sources_jobs(conn, monkeypatch):
    scan(conn, monkeypatch, Acme=["a1"], Bolt=["b1"])
    scan(conn, monkeypatch, Acme=["a1"], Bolt=SourceError("timeout"))
    scan(conn, monkeypatch, Acme=["a1"], Bolt=SourceError("timeout"))
    assert lifecycle(conn)["b1"] == "open"


# ---------------------------------------------------------------- suspicious zero

def test_sudden_zero_is_suspicious_and_closes_nothing(conn, monkeypatch):
    for _ in range(3):
        scan(conn, monkeypatch, Acme=["a1", "a2", "a3", "a4"], Bolt=[])
    run_id = scan(conn, monkeypatch, Acme=[], Bolt=[])
    result = conn.execute("SELECT result, error_reason FROM source_runs WHERE run_id = ? AND source = 'Acme'",
                          (run_id,)).fetchone()
    assert tuple(result) == ("suspicious", "zero_jobs")
    assert set(lifecycle(conn).values()) == {"open"}


def test_zero_is_believed_after_three_runs_in_a_row(conn, monkeypatch):
    for _ in range(3):
        scan(conn, monkeypatch, Acme=["a1", "a2", "a3", "a4"], Bolt=[])
    for _ in range(2):
        scan(conn, monkeypatch, Acme=[], Bolt=[])
    assert set(lifecycle(conn).values()) == {"open"}
    scan(conn, monkeypatch, Acme=[], Bolt=[])  # third zero in a row: accepted as a real scan
    assert set(lifecycle(conn).values()) == {"possibly_closed"}


def test_small_sources_can_drop_to_zero(conn, monkeypatch):
    scan(conn, monkeypatch, Acme=["a1"], Bolt=[])
    scan(conn, monkeypatch, Acme=[], Bolt=[])
    assert lifecycle(conn) == {"a1": "possibly_closed"}


# ---------------------------------------------------------------- alerts

def test_same_alert_is_sent_once_a_day(conn, owner):
    health.alert(conn, "claude_logged_out", "log in")
    health.alert(conn, "claude_logged_out", "log in")
    health.alert(conn, "run_failed", "offline")
    assert len(owner) == 2


def test_run_with_no_sources_scanned_alerts(conn, monkeypatch, owner):
    feeds(monkeypatch, Acme=SourceError("network", "x"), Bolt=SourceError("network", "x"))
    main.daily(conn)
    assert any("No source could be scanned" in m and "network" in m for m in owner)
    assert conn.execute("SELECT state FROM runs").fetchone()[0] == "failed"


def test_logged_out_claude_alerts(conn, owner):
    run_id = store.start_run(conn)
    health.check_run(conn, run_id, 2, 2, {"asha": {"stopped": "logged_out"}}, {})
    assert len(owner) == 1 and "logged out" in owner[0]


def test_crash_is_reported_and_run_marked_failed(conn, monkeypatch, owner):
    feeds(monkeypatch, Acme=["a1"], Bolt=["b1"])
    monkeypatch.setattr(main, "match", lambda conn, run_id: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        main.daily(conn)
    assert "crashed" in owner[0] and "ZeroDivisionError" in owner[0]
    assert conn.execute("SELECT state FROM runs").fetchone()[0] == "failed"


def test_stale_daily_run_alerts(conn, owner):
    run_id = store.start_run(conn)
    store.finish_run(conn, run_id, "done")
    health.check_stale(conn)
    assert owner == []
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE runs SET finished = ?", (two_days_ago,))
    conn.commit()
    health.check_stale(conn)
    assert len(owner) == 1 and "hasn't finished" in owner[0]


def test_alert_without_operator_chat_is_printed(conn, monkeypatch, capsys):
    monkeypatch.delenv("OPERATOR_TELEGRAM_CHAT_ID", raising=False)
    health.alert(conn, "x", "something broke")
    assert "something broke" in capsys.readouterr().out
    assert store.get_state(conn, "alert:x") is None  # not marked sent, so it is tried again next time


# ---------------------------------------------------------------- weekly health

def test_broken_source_appears_in_health_summary(conn, monkeypatch):
    scan(conn, monkeypatch, Acme=["a1"], Bolt=["b1"])
    scan(conn, monkeypatch, Acme=["a1"], Bolt=SourceError("not_found", "x"))
    text = health.summary(conn, [])
    assert "Bolt: 1/2 ok — not_found" in text
    assert "Acme" not in text.split("Sources:")[1].split("AI:")[0].replace("1 of 2", "")
    assert "scan success 75%" in text


def test_weekly_summary_waits_a_week(conn, owner):
    assert not health.weekly_if_due(conn, [])          # first run only starts the clock
    assert not health.weekly_if_due(conn, [])
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    store.set_state(conn, "health_last_sent", week_ago)
    assert health.weekly_if_due(conn, [])
    assert "last 7 days" in owner[0]


# ---------------------------------------------------------------- retention

def test_old_logs_are_deleted_but_jobs_kept(conn, monkeypatch):
    old_run = scan(conn, monkeypatch, Acme=["a1"], Bolt=["b1"])
    store.record_llm_call(conn, old_run, "asha", 10, 10, 5.0, None)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    conn.execute("UPDATE runs SET started = ?", (long_ago,))
    conn.execute("UPDATE llm_calls SET at = ?", (long_ago,))
    conn.commit()
    new_run = scan(conn, monkeypatch, Acme=["a1"], Bolt=["b1"])
    removed = store.delete_old_logs(conn)
    assert removed == {"runs": 1, "source_runs": 2, "llm_calls": 1}
    assert [r[0] for r in conn.execute("SELECT run_id FROM runs")] == [new_run]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_big_log_files_are_trimmed(conn, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "daily.log").write_bytes(b"line\n" * (main.MAX_LOG_BYTES // 4))
    main.cleanup(conn)
    data = (logs / "daily.log").read_bytes()
    assert len(data) <= main.MAX_LOG_BYTES // 2 + 100 and data.startswith(b"[older lines removed]\nline\n")


# ---------------------------------------------------------------- parallel lanes

def test_different_systems_run_at_the_same_time_and_one_system_runs_one_at_a_time(tmp_path, monkeypatch):
    import threading

    srcs = [Source(name=f"L{i}", type="lever", board=f"l{i}") for i in range(3)] + \
           [Source(name=f"G{i}", type="greenhouse", board=f"g{i}") for i in range(3)]
    c = store.connect(tmp_path / "p.db")
    monkeypatch.setattr(main, "load_sources", lambda: srcs)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    lock, active, peak = threading.Lock(), {"lever": 0, "greenhouse": 0}, {"lever": 0, "greenhouse": 0, "all": 0}
    both = threading.Barrier(2, timeout=5)  # passes only if a lever and a greenhouse source run together

    def fetch(source, known=frozenset()):
        with lock:
            active[source.type] += 1
            peak[source.type] = max(peak[source.type], active[source.type])
            peak["all"] = max(peak["all"], sum(active.values()))
        if source.name in ("L0", "G0"):
            both.wait()
        with lock:
            active[source.type] -= 1
        return [job(source, "1")]

    monkeypatch.setattr(main, "ADAPTERS", {"lever": fetch, "greenhouse": fetch})
    assert main.collect(c, store.start_run(c)) == (6, 6)
    assert peak == {"lever": 1, "greenhouse": 1, "all": 2}


def test_a_crashing_source_in_a_lane_does_not_stop_the_rest(conn, monkeypatch):
    scan(conn, monkeypatch, Acme=ValueError("feed changed shape"), Bolt=["b1"])
    rows = dict(conn.execute("SELECT source, result FROM source_runs").fetchall())
    assert rows == {"Acme": "failed", "Bolt": "ok"}
    detail = conn.execute("SELECT error_detail FROM source_runs WHERE source = 'Acme'").fetchone()[0]
    assert "feed changed shape" in detail and "Traceback" in detail


def test_long_closed_jobs_are_deleted_unless_someone_applied(conn, monkeypatch):
    scan(conn, monkeypatch, Acme=["a1", "a2", "a3"], Bolt=["b1"])
    keys = [r[0] for r in conn.execute("SELECT job_key FROM jobs WHERE source = 'Acme' ORDER BY job_key")]
    ids = store.share_ids(conn, keys)
    store.mark_applied(conn, 42, ids[keys[1]])
    long_ago = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    conn.execute("UPDATE jobs SET lifecycle = 'closed', last_seen = ? WHERE source = 'Acme'", (long_ago,))
    conn.execute("UPDATE jobs SET lifecycle = 'closed' WHERE job_key = ?", (keys[2],))  # closed, seen recently
    conn.execute("UPDATE jobs SET last_seen = ? WHERE job_key = ?", (datetime.now(timezone.utc).isoformat(), keys[2]))
    conn.commit()
    removed = store.delete_old_jobs(conn)
    assert removed == {"jobs": 1, "matches": 0, "job numbers": 1}
    left = {r[0] for r in conn.execute("SELECT job_key FROM jobs")}
    assert keys[0] not in left and {keys[1], keys[2]} <= left
    assert [r["title"] for r in store.applied_jobs(conn, 42)] == ["Backend Engineer"]
    assert store.shared_job(conn, ids[keys[0]]) is None
