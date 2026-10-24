"""Jibe (AMD, GitHub), Netflix's older Eightfold API and the remote job boards parse saved real feeds."""

import json
from pathlib import Path

from src import digest
from src.collect import eightfold, jibe, remoteboards as rb
from src.models import Source

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def check(job):
    assert job.title and job.ats_id and job.location_raw and job.company
    assert str(job.apply_url).startswith("https://")
    assert job.posted_at is not None
    assert len(job.description) > 100 and "<p>" not in job.description


def test_jibe_india_job_with_full_description():
    src = Source(name="AMD", type="jibe", board="https://careers.amd.com/")
    job = jibe.to_job(load("jibe_amd.json"), src)
    check(job)
    assert job.india_eligible == "yes" and job.cities
    assert str(job.apply_url) == f"https://careers.amd.com/careers-home/jobs/{job.ats_id}"


def test_jibe_job_page_fallback():
    assert digest.safe_apply_url("http://x.example/j", "jibe", "https://careers.amd.com", "89389", set()) \
        == "https://careers.amd.com/careers-home/jobs/89389"


def test_netflix_reads_older_eightfold_api():
    src = Source(name="Netflix", type="eightfold", board="https://explore.jobs.netflix.net/careers?domain=netflix.com")
    f = load("eightfold_netflix_legacy.json")
    job = eightfold.legacy_job(f["item"], f["detail"], src)
    check(job)
    assert job.india_eligible == "yes"
    assert "explore.jobs.netflix.net" in str(job.apply_url)


def test_remoteok_skips_legal_notice_and_keeps_board_link(monkeypatch):
    raw = load("remoteok.json")
    monkeypatch.setattr(rb, "get_json", lambda url, params=None: [{"legal": "notice"}] + raw)
    monkeypatch.setattr(rb, "MAX_AGE_DAYS", 100000)
    jobs = rb.fetch_remoteok(Source(name="Remote OK", type="remoteok", board=""))
    assert len(jobs) == len(raw)
    for job in jobs:
        check(job)
        assert job.work_mode == "remote" and "remoteok.com" in str(job.apply_url).lower()


def test_remotive_worldwide_job_is_india_eligible():
    job = rb.remotive_job(load("remotive.json")[1], Source(name="Remotive", type="remotive", board="software-dev"))
    check(job)
    assert job.location_raw == "Remote - Worldwide" and job.india_eligible in ("yes", "unclear")


def test_working_nomads_region_list():
    job = rb.workingnomads_job(load("workingnomads.json")[0], Source(name="WN", type="workingnomads", board=""))
    check(job)
    assert job.company == "Lemon.io"


def test_jobspresso_splits_company_and_place():
    items = rb._rss_items((FIX / "jobspresso.rss").read_text())
    job = rb.jobspresso_job(items[0], Source(name="Jobspresso", type="jobspresso", board=""))
    check(job)
    assert "<" not in job.company and "⚲" not in job.location_raw
    assert job.location_raw.startswith("Remote")

