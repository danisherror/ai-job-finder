"""SmartRecruiters, Workday, Workable and Himalayas adapters parse saved real feeds, and the store
handles details that weren't re-fetched and jobs listed both on a job board and a company feed."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import digest, store
from src.collect import himalayas, smartrecruiters, workable, workday
from src.models import Job, Source

FIX = Path(__file__).parent / "fixtures"
SR = Source(name="Freshworks", type="smartrecruiters", board="Freshworks")
WD = Source(name="NVIDIA", type="workday", board="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
WK = Source(name="Hugging Face", type="workable", board="huggingface")
HM = Source(name="Himalayas", type="himalayas", board="India")
CO = Source(name="Acme", type="greenhouse", board="acme")


def load(name):
    return json.loads((FIX / name).read_text())


def check(job: Job):
    assert job.title and job.ats_id and job.location_raw
    assert str(job.apply_url).startswith("https://")
    assert job.posted_at is not None
    assert len(job.description) > 100 and "<p>" not in job.description


def test_smartrecruiters_india_job():
    f = load("smartrecruiters_freshworks.json")
    job = smartrecruiters.to_job(f["item"], f["detail"], SR)
    check(job)
    assert job.india_eligible == "yes" and job.cities == ["Bengaluru"] and job.work_mode == "hybrid"
    assert "Freshworks is" not in job.description[:200]  # company blurb left out


def test_known_smartrecruiters_job_has_no_description():
    f = load("smartrecruiters_freshworks.json")
    job = smartrecruiters.to_job(f["item"], None, SR)
    assert job.description == "" and str(job.apply_url).startswith("https://jobs.smartrecruiters.com/Freshworks/")


def test_workday_job_and_india_filter():
    f = load("workday_nvidia.json")
    job = workday.to_job(f["item"], f["detail"], WD, "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
    check(job)
    assert job.india_eligible == "yes" and job.cities == ["Bengaluru"]
    param, ids = workday.india_facet(f["facets"])
    assert param == "locationHierarchy1" and len(ids) == 1


def test_workday_site_urls():
    api, public = workday.site_urls("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced")
    assert api == "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced"
    assert public == "https://adobe.wd5.myworkdayjobs.com/external_experienced"


def test_workday_falls_back_to_city_values():
    facets = [{"facetParameter": "locations", "values": [
        {"descriptor": "India, Pune", "id": "a", "count": 3},
        {"descriptor": "India, Bengaluru", "id": "b", "count": 5},
        {"descriptor": "Bengaluru, Karnātaka, India", "id": "d", "count": 184},   # HPE's order
        {"descriptor": "All, Indiana, United States of America", "id": "e", "count": 1},
        {"descriptor": "US, Austin", "id": "c", "count": 9}]}]
    assert workday.india_facet(facets) == ("locations", ["a", "b", "d"])
    assert workday.india_facet([{"facetParameter": "locations", "values": [{"descriptor": "US", "id": "c"}]}]) is None


def test_workable_jobs():
    jobs = [workable.to_job(r, WK) for r in load("workable_huggingface.json")["jobs"]]
    assert len(jobs) == 3
    for j in jobs:
        check(j)


def test_himalayas_jobs_keep_real_company_and_board_link():
    jobs = [himalayas.to_job(r, HM) for r in load("himalayas_india.json")["jobs"]]
    assert len(jobs) == 4
    for j in jobs:
        check(j)
        assert j.company != "Himalayas" and j.work_mode == "remote"
        assert "himalayas.app" in str(j.apply_url)
    assert all(j.india_eligible == "yes" for j in jobs if "India" in j.location_raw)


# ---------------------------------------------------------------- store

@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.sync_sources(c, [SR, HM, CO])
    return c


def job(source: Source, ats_id: str, title="Backend Engineer", company="Acme", description="x" * 200, days_old=1):
    return Job(source=source.name, ats_id=ats_id, title=title, company=company, location_raw="Bengaluru, India",
               description=description, apply_url=f"https://example.com/{ats_id}",
               posted_at=datetime.now(timezone.utc) - timedelta(days=days_old))


def save(conn, source, jobs):
    return store.save_source_result(conn, store.start_run(conn), source, store.now(), jobs)


def lifecycle(conn):
    return dict(conn.execute("SELECT ats_id, lifecycle FROM jobs").fetchall())


def test_known_job_without_description_keeps_stored_details(conn):
    save(conn, SR, [job(SR, "1", description="Python, 3+ years of experience. " * 10)])
    assert store.known_ats_ids(conn, "Freshworks") == {"1"}
    save(conn, SR, [job(SR, "1", description="")])
    desc, exp = conn.execute("SELECT description, experience_min FROM jobs").fetchone()
    assert desc.startswith("Python") and exp is None  # experience_min untouched (job() doesn't parse it)
    assert lifecycle(conn) == {"1": "open"}


def test_job_board_copy_of_company_job_is_skipped(conn):
    save(conn, CO, [job(CO, "co-1", title="Backend Engineer", company="Acme")])
    new = save(conn, HM, [job(HM, "hm-1", title="Backend Engineer ", company="Acme Technologies Pvt Ltd"),
                          job(HM, "hm-2", title="Data Engineer", company="Other Co")])
    assert new == 1
    assert set(lifecycle(conn)) == {"co-1", "hm-2"}


def test_company_feed_closes_earlier_job_board_copy(conn):
    save(conn, HM, [job(HM, "hm-1", company="Acme Inc.")])
    save(conn, CO, [job(CO, "co-1", company="Acme")])
    assert lifecycle(conn) == {"hm-1": "closed", "co-1": "open"}


def test_job_board_jobs_close_by_age_not_by_misses(conn):
    save(conn, HM, [job(HM, "fresh", title="A"), job(HM, "old", title="B", days_old=40)])
    save(conn, HM, [])
    save(conn, HM, [])
    assert lifecycle(conn) == {"fresh": "open", "old": "closed"}


@pytest.mark.parametrize("source_type,board,ats_id,expected", [
    ("workday", "https://nvidia.wd5.myworkdayjobs.com/Site", "Eng_JR1", "https://nvidia.wd5.myworkdayjobs.com/Site/details/Eng_JR1"),
    ("smartrecruiters", "Freshworks", "744", "https://jobs.smartrecruiters.com/Freshworks/744"),
    ("himalayas", "India", "https://himalayas.app/companies/a/jobs/b", "https://himalayas.app/companies/a/jobs/b"),
])
def test_unsafe_apply_links_fall_back_to_the_job_page(source_type, board, ats_id, expected):
    assert digest.safe_apply_url("http://evil.example/x", source_type, board, ats_id, set()) == expected


def test_workday_skips_stub_postings(monkeypatch):
    stub = {"bulletFields": ["R00272731", "Gurugram, Ddc1A"]}
    real = load("workday_nvidia.json")["item"]
    facets = [{"facetParameter": "locationCountry", "values": [{"descriptor": "India", "id": "in"}]}]
    replies = iter([{"facets": facets}, {"total": 2, "jobPostings": [stub, real]}, {"jobPostings": []}])
    monkeypatch.setattr(workday, "post_json", lambda url, body: next(replies))
    jobs = workday.fetch(WD, known=frozenset({workday.job_id(real)}))
    assert [j.ats_id for j in jobs] == [workday.job_id(real)]


def test_workday_respects_a_per_company_cap(monkeypatch):
    real = load("workday_nvidia.json")["item"]
    page = [dict(real, externalPath=f"/job/x/Eng_JR{i}") for i in range(20)]
    facets = [{"facetParameter": "locationCountry", "values": [{"descriptor": "India", "id": "in"}]}]
    replies = iter([{"facets": facets}, {"total": 100, "jobPostings": page}, {"jobPostings": page}])
    monkeypatch.setattr(workday, "post_json", lambda url, body: next(replies))
    capped = WD.model_copy(update={"max_jobs": 25})
    jobs = workday.fetch(capped, known=frozenset(workday.job_id(p) for p in page))
    assert len(jobs) == 25


def test_workday_reads_country_code_city_values():
    facets = [{"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locations", "values": [
        {"id": "a", "descriptor": "IND-Bangalore Electronic City - S1"},
        {"id": "b", "descriptor": "USA-CA San Jose"},
        {"id": "c", "descriptor": "INDIANA-Remote"}]}]}]
    assert workday.india_facet(facets) == ("locations", ["a"])
