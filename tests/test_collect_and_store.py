"""Adapters parse saved real feeds; the store never duplicates and handles backfill."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import store
from src.collect import ashby, greenhouse, lever
from src.models import Job, Source

FIX = Path(__file__).parent / "fixtures"
GH = Source(name="HackerRank", type="greenhouse", board="hackerrank")
LV = Source(name="Zeta", type="lever", board="zeta")
AB = Source(name="Sarvam AI", type="ashby", board="sarvam")


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.sync_sources(c, [GH, LV, AB])
    return c


def gh_jobs():
    return [greenhouse.to_job(r, GH) for r in load("greenhouse_hackerrank.json")["jobs"]]


def lv_jobs():
    return [lever.to_job(r, LV) for r in load("lever_zeta.json")]


def ab_jobs():
    return [ashby.to_job(r, AB) for r in load("ashby_sarvam.json")["jobs"]]


@pytest.mark.parametrize("jobs_fn", [gh_jobs, lv_jobs, ab_jobs])
def test_adapters_parse_real_feeds(jobs_fn):
    jobs = jobs_fn()
    assert len(jobs) == 4
    for j in jobs:
        assert j.title and j.ats_id and j.location_raw
        assert str(j.apply_url).startswith("https://")
        assert j.posted_at is not None
        assert len(j.description) > 100
        assert "<p>" not in j.description


def test_lever_and_ashby_india_jobs_are_eligible():
    assert all(j.india_eligible == "yes" for j in lv_jobs() if "Bangalore" in j.location_raw)
    assert all(j.india_eligible == "yes" for j in ab_jobs() if j.cities)


def run_once(conn, source, jobs):
    run_id = store.start_run(conn)
    return store.save_source_result(conn, run_id, source, store.now(), jobs)


def test_second_run_adds_no_duplicates(conn):
    assert run_once(conn, GH, gh_jobs()) == 4
    assert run_once(conn, GH, gh_jobs()) == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4


def test_job_key_uses_ats_id():
    job = gh_jobs()[0]
    assert store.job_key("greenhouse", "hackerrank", job) == f"greenhouse:hackerrank:{job.ats_id}"


def make_job(ats_id, days_old):
    return Job(source="HackerRank", ats_id=ats_id, title="Engineer", company="HackerRank",
               location_raw="Bengaluru", apply_url=f"https://example.com/{ats_id}",
               posted_at=datetime.now(timezone.utc) - timedelta(days=days_old),
               updated_at=datetime.now(timezone.utc))  # updated today, like Stripe's feed


def test_first_scan_marks_old_jobs_as_backfill_using_posted_date(conn):
    run_once(conn, GH, [make_job("old", 30), make_job("new", 2)])
    rows = dict(conn.execute("SELECT ats_id, backfill FROM jobs").fetchall())
    assert rows == {"old": 1, "new": 0}


def test_jobs_found_after_first_scan_are_never_backfill(conn):
    run_once(conn, GH, [make_job("a", 30)])
    run_once(conn, GH, [make_job("a", 30), make_job("b", 30)])  # old posting, but new to us
    rows = dict(conn.execute("SELECT ats_id, backfill FROM jobs").fetchall())
    assert rows == {"a": 1, "b": 0}


def test_jobs_and_done_mark_are_saved_together(conn):
    """If saving fails partway, neither the jobs nor the 'ok' record are written."""
    bad = make_job("x", 1)
    jobs = [make_job("y", 1), bad]
    object.__setattr__(bad, "title", None)  # violates NOT NULL inside the transaction
    run_id = store.start_run(conn)
    with pytest.raises(Exception):
        store.save_source_result(conn, run_id, GH, store.now(), jobs)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0
