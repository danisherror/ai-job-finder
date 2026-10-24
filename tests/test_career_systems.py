"""Atlassian, RippleHire, Phenom, Avature, the sitemap readers (TalentBrew, iCIMS, Societe Generale),
Jobicy and Adzuna parse saved real responses (Adzuna's is built from its documented format)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import digest, store
from src.collect import ADAPTERS, atlassian, avature, jobpages, phenom, portals, remoteboards, ripplehire
from src.models import Job, Source

FIX = Path(__file__).parent / "fixtures"
ATL = Source(name="Atlassian", type="atlassian", board="")
LTI = Source(name="LTIMindtree", type="ripplehire",
             board="https://ltimindtree.ripplehire.com/candidate/?token=xviyQvbnyYZdGtozXoNm#list/geo=India")
NTT = Source(name="NTT DATA", type="phenom", board="https://careers.nttdata.com/global/en")
SIE = Source(name="Siemens", type="avature", board="https://jobs.siemens.com/en_US/externaljobs?search=India")
SNPS = Source(name="Synopsys", type="talentbrew", board="https://careers.synopsys.com")
IDEAS = Source(name="IDeaS", type="icims", board="https://ideasglobal-sas.icims.com")
SG = Source(name="Societe Generale", type="socgen", board="https://careers.societegenerale.com")
JOBICY = Source(name="Jobicy", type="jobicy", board="")
ADZUNA = Source(name="Adzuna", type="adzuna", board="developer|devops")


def load(name):
    return json.loads((FIX / name).read_text())


def text(name):
    return (FIX / name).read_text()


def check(job: Job):
    assert job.title and job.ats_id and job.location_raw
    assert str(job.apply_url).startswith("https://")
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert len(job.description) > 100 and "<p>" not in job.description and "<br" not in job.description
    assert job.india_eligible == "yes"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    for module in (ripplehire, phenom, avature, jobpages):
        monkeypatch.setattr(module, "DELAY", 0)
    monkeypatch.setattr(portals, "PAGE_DELAY", 0)


# ---------------------------------------------------------------- Atlassian

def test_atlassian_keeps_india_jobs_with_full_description(monkeypatch):
    monkeypatch.setattr(atlassian, "get_json", lambda url: load("atlassian_listings.json"))
    jobs = atlassian.fetch(ATL)
    assert len(jobs) == 2                      # the Sydney job is dropped
    for j in jobs:
        check(j)
        assert ".icims.com/jobs/" in str(j.apply_url)
    remote, office = jobs
    assert remote.location_raw == "Remote, India" and remote.work_mode == "remote"
    assert office.location_raw == "Bengaluru, India | Remote, India" and office.cities == ["Bengaluru"]


# ---------------------------------------------------------------- RippleHire

def test_ripplehire_board_gives_site_token_and_filters():
    assert ripplehire.site(LTI.board) == ("https://ltimindtree.ripplehire.com", "xviyQvbnyYZdGtozXoNm", {"geo": "India"})
    assert ripplehire.site("https://mphasis.ripplehire.com/candidate/?token=abc#list")[2] == {}


def test_ripplehire_job_with_detail():
    f = load("ripplehire_ltimindtree.json")
    job = ripplehire.to_job(f["search"]["jobVoList"][0], f["detail"], LTI)
    check(job)
    assert job.location_raw == "Bengaluru, India" and job.cities == ["Bengaluru"]
    assert (job.experience_min, job.experience_max) == (8, 12)
    assert str(job.apply_url).endswith("#detail/job/903856")


def test_ripplehire_fetch_opens_only_new_jobs(monkeypatch):
    f = load("ripplehire_ltimindtree.json")
    searches, details = [], []

    def fake_search(url, form, headers=None):
        searches.append(json.loads(form["careerSiteUrlParams"]))
        return {**f["search"], "totalJobCount": 3} if len(searches) == 1 else {"jobVoList": []}

    def fake_detail(url, headers=None, params=None):
        details.append(params["jobSeq"])
        return {"jobVO": f["detail"]}

    monkeypatch.setattr(ripplehire, "post_form", fake_search)
    monkeypatch.setattr(ripplehire, "get_json", fake_detail)
    jobs = ripplehire.fetch(LTI, known=frozenset({"903848", "903846"}))
    assert len(jobs) == 3 and details == ["903856"]
    assert searches[0]["geo"] == "India" and searches[0]["token"] == "xviyQvbnyYZdGtozXoNm"
    assert [bool(j.description) for j in jobs] == [True, False, False]


# ---------------------------------------------------------------- Phenom

def test_phenom_reads_site_settings_from_search_page(monkeypatch):
    monkeypatch.setattr(phenom, "get_text", lambda url: text("phenom_search_page.html"))
    assert phenom.settings(NTT.board) == {"refNum": "NTT1GLOBAL", "locale": "en_global", "country": "global"}


def test_phenom_job_and_fetch(monkeypatch):
    f = load("phenom_nttdata.json")
    calls = []

    def fake_post(url, body, headers=None):
        calls.append(body["ddoKey"])
        if body["ddoKey"] == "jobDetail":
            return {"jobDetail": {"data": {"job": f["detail"]}}}
        return f["search"] if body["from"] == 0 else {"refineSearch": {"totalHits": 3, "data": {"jobs": []}}}

    monkeypatch.setattr(phenom, "get_text", lambda url: text("phenom_search_page.html"))
    monkeypatch.setattr(phenom, "post_json", fake_post)
    jobs = phenom.fetch(NTT, known=frozenset({"P-100057", "P-100174"}))
    assert calls == ["refineSearch", "jobDetail", "refineSearch"]
    check(jobs[0])
    assert str(jobs[0].apply_url) == "https://careers.nttdata.com/global/en/job/R-128662"
    assert jobs[2].location_raw == "India" and jobs[2].india_eligible == "yes"


# ---------------------------------------------------------------- Avature

def test_avature_list_and_job_page():
    rows = avature.list_page(text("avature_siemens_list.html"))
    assert len(rows) == 6 and all(r[1].startswith("https://jobs.siemens.com/") for r in rows)
    job_id, url, title = rows[0]
    job = avature.to_job(job_id, url, title, text("avature_siemens_job.html"), SIE)
    check(job)
    assert "India" in job.location_raw


def test_avature_reads_every_page_but_opens_at_most_max_new(monkeypatch):
    page = text("avature_siemens_list.html").replace('aria-label="912 results"', 'aria-label="12 results"')
    opened = []

    def fake_get(url, params=None):
        if "SearchJobs" in url:
            return page if params["folderOffset"] < 12 else ""
        opened.append(url)
        return text("avature_siemens_job.html")

    monkeypatch.setattr(avature, "get_text", fake_get)
    monkeypatch.setattr(avature, "MAX_NEW", 4)
    ids = [r[0] for r in avature.list_page(page)]
    jobs = avature.fetch(SIE, known=frozenset(ids[:1]))
    assert len(opened) == 4
    # the same 6 jobs appear on both fake pages: 1 known each time + 4 opened on the first page
    assert len(jobs) == 2 + 4 and sum(1 for j in jobs if j.description) == 4


# ---------------------------------------------------------------- sitemap + job pages

@pytest.mark.parametrize("fixture,source,place", [
    ("talentbrew_synopsys_job.html", SNPS, "Hyderabad, India"),
    ("socgen_job.html", SG, "Bangalore, India"),
])
def test_job_posting_pages(fixture, source, place):
    job = jobpages.to_job("1", "https://example.com/job/1", jobpages.job_posting(text(fixture)), source)
    check(job)
    assert job.location_raw == place


def test_icims_job_outside_india_drops_placeholders():
    job = jobpages.to_job("41212", "https://ideasglobal-sas.icims.com/jobs/41212/x/job",
                          jobpages.job_posting(text("icims_ideas_job.html")), IDEAS)
    assert job.location_raw == "Dubai, AE" and job.india_eligible == "no" and job.posted_at


def test_talentbrew_picks_india_cities_from_urls():
    urls = ["https://careers.arm.com/job/bengaluru/software-engineer/33099/1",
            "https://careers.arm.com/job/indianapolis/nurse/33099/2",
            "https://careers.arm.com/job/new-delhi/analyst/33099/3",
            "https://careers.arm.com/job/cambridge/engineer/33099/4",
            "https://careers.arm.com/category/software-engineering-jobs/33099/8081696/1"]
    assert [i for i, _ in jobpages.talentbrew_pages(urls)] == ["1", "3"]


def test_icims_and_socgen_pages():
    assert jobpages.icims_pages(["https://x.icims.com/jobs/intro",
                                 "https://x.icims.com/jobs/42976/sr-manager/job"]) == [
        ("42976", "https://x.icims.com/jobs/42976/sr-manager/job")]
    india = jobpages.socgen_india_refs(text("socgen_home.html"))
    assert india == {"26000HCO"}
    urls = ["https://careers.societegenerale.com/en/job-offers/analyst-data-quality-26000HCO-en",
            "https://careers.societegenerale.com/offres-d-emploi/analyste-26000HCO-fr",
            "https://careers.societegenerale.com/en/job-offers/trader-25000N0E-en"]
    assert jobpages.socgen_pages(urls, india) == [("26000HCO", urls[0])]


def test_read_pages_returns_known_jobs_bare_and_caps_new_ones(monkeypatch):
    opened = []
    monkeypatch.setattr(jobpages, "get_text", lambda url, params=None: opened.append(url) or text("socgen_job.html"))
    monkeypatch.setattr(jobpages, "MAX_NEW", 2)
    pages = [(str(i), f"https://careers.societegenerale.com/en/job-offers/x-{i}-en") for i in range(5)]
    jobs = jobpages.read_pages(SG, pages, known=frozenset({"0"}))
    assert len(opened) == 2 and len(jobs) == 3
    assert jobs[0].title == "" and jobs[0].description == ""


# ---------------------------------------------------------------- store: known jobs without a title

@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.sync_sources(c, [SNPS])
    return c


def test_known_job_without_title_keeps_saved_title(conn):
    def save(job):
        store.save_source_result(conn, store.start_run(conn), SNPS, store.now(), [job])

    page = jobpages.job_posting(text("talentbrew_synopsys_job.html"))
    save(jobpages.to_job("94055936032", "https://careers.synopsys.com/job/hyderabad/x/44408/94055936032", page, SNPS))
    save(jobpages.to_job("94055936032", "https://careers.synopsys.com/job/hyderabad/x/44408/94055936032", None, SNPS))
    title, desc, lifecycle = conn.execute("SELECT title, description, lifecycle FROM jobs").fetchone()
    assert title == "Salesforce Developer" and len(desc) > 100 and lifecycle == "open"


# ---------------------------------------------------------------- Jobicy and Adzuna

def test_jobicy_worldwide_jobs_are_open_to_india():
    jobs = [remoteboards.jobicy_job(r, JOBICY) for r in load("jobicy.json")["jobs"]]
    assert [j.india_eligible for j in jobs] == ["yes", "yes", "no"]
    assert all(str(j.apply_url).startswith("https://jobicy.com/jobs/") and len(j.description) > 100 for j in jobs)


ADZUNA_RESULT = {   # the shape documented at developer.adzuna.com (search results)
    "id": "4893216421", "title": "Senior <strong>Python</strong> Developer",
    "company": {"display_name": "Acme Software"},
    "location": {"display_name": "Bangalore, Karnataka", "area": ["India", "Karnataka", "Bangalore"]},
    "description": "We need a Python developer with 4-6 years of experience building APIs with Django and Postgres. " * 2,
    "redirect_url": "https://www.adzuna.in/land/ad/4893216421?se=abc", "created": "2026-10-22T09:15:00Z",
    "salary_min": 1800000, "salary_max": 2600000, "salary_is_predicted": "0",
    "category": {"label": "IT Jobs", "tag": "it-jobs"},
}


def test_adzuna_job():
    job = portals.adzuna_job(ADZUNA_RESULT, ADZUNA)
    assert job.title == "Senior Python Developer" and job.company == "Acme Software"
    assert job.location_raw == "Bangalore, Karnataka, India" and job.cities == ["Bengaluru"]
    assert job.salary == "INR 1,800,000-2,600,000 yearly" and job.posted_at.tzinfo is not None
    assert (job.experience_min, job.experience_max) == (4, 6)
    assert portals.adzuna_job({**ADZUNA_RESULT, "salary_is_predicted": "1"}, ADZUNA).salary is None


def test_adzuna_needs_both_keys_and_reads_each_keyword(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    calls = []
    monkeypatch.setattr(portals, "get_json", lambda url, params=None, headers=None:
                        calls.append(params) or {"results": [ADZUNA_RESULT]})
    jobs = portals.fetch_adzuna(ADZUNA)
    assert [c["what"] for c in calls] == ["developer", "devops"] and len(jobs) == 1
    assert calls[0]["app_id"] == "id" and calls[0]["max_days_old"] == portals.MAX_AGE_DAYS


# ---------------------------------------------------------------- wiring

@pytest.mark.parametrize("kind", ["atlassian", "ripplehire", "phenom", "avature", "talentbrew", "icims", "socgen",
                                  "jobicy", "adzuna"])
def test_new_types_have_an_adapter_and_a_fallback_link(kind):
    assert kind in ADAPTERS
    job_id = "https://jobicy.com/jobs/123-x" if kind == "jobicy" else "123"   # Jobicy's ID is the job's URL
    link = digest.safe_apply_url("http://elsewhere.example/job", kind, "https://example.com/site?search=x", job_id, set())
    assert link.startswith("https://")
