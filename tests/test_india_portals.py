"""Job portals read through their own web pages (LinkedIn, Naukri via AmbitionBox, Foundit, ...) parse saved
real responses, and their fetchers page and stop the way they should."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.collect import indiaportals as ip
from src.collect import jobpages
from src.collect.http import SourceError
from src.models import Source

FIX = Path(__file__).parent / "fixtures"


def src(kind: str, board: str = "") -> Source:
    return Source(name=kind.title(), type=kind, board=board)


def text(name):
    return (FIX / name).read_text()


def load(name):
    return json.loads(text(name))


def check(job, description=True, dated=True):
    assert job.title and job.ats_id and job.company not in ("", "Unknown")
    assert str(job.apply_url).startswith("https://")
    assert job.india_eligible == "yes", job.location_raw
    assert job.location_raw
    if dated:
        assert job.posted_at is not None
    if description:
        assert len(job.description) > 20 and "<p>" not in job.description and "&amp;" not in job.description


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    for name in ("PAGE_DELAY", "LINKEDIN_DELAY"):
        monkeypatch.setattr(ip, name, 0)
    monkeypatch.setattr(ip, "MAX_AGE_DAYS", 100000)
    monkeypatch.setattr(jobpages, "DELAY", 0)


def test_ago_understands_relative_dates():
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    assert ip.ago("Today", now) == now and ip.ago("Few hours ago", now) == now and ip.ago("5 hours ago", now) == now
    assert ip.ago("1 day ago", now) == now - timedelta(days=1)
    assert ip.ago("2 weeks ago", now) == now - timedelta(days=14)
    assert ip.ago("30+ days ago", now) == now - timedelta(days=30)
    assert ip.ago("", now) is None and ip.ago("Actively hiring", now) is None


# ---------------------------------------------------------------- LinkedIn

def test_linkedin_cards_and_job_page():
    cards = ip.linkedin_cards(text("linkedin_search.html"))
    assert len(cards) == 3 and all(c["id"].isdigit() and c["title"] and c["company"] for c in cards)
    job = ip.linkedin_job(cards[0], src("linkedin"), text("linkedin_job.html"))
    check(job)
    assert str(job.apply_url) == f"https://www.linkedin.com/jobs/view/{cards[0]['id']}"
    assert "Employment type:" in job.description          # the criteria list is kept


def test_linkedin_opens_only_new_jobs_and_stops_when_rate_limited(monkeypatch):
    search, page = text("linkedin_search.html"), text("linkedin_job.html")
    ids = [c["id"] for c in ip.linkedin_cards(search)]
    opened = []

    def fake_get(url, params=None, headers=None, rate_limit_retries=None):
        if url == ip.LINKEDIN_SEARCH:
            return search
        opened.append(url)
        if len(opened) == 2:
            raise SourceError("rate_limited", "HTTP 429")
        return page

    monkeypatch.setattr(ip, "get_text", fake_get)
    jobs = ip.fetch_linkedin(src("linkedin", "software engineer"), known=frozenset(ids[:1]))
    # The known job comes back bare; the first new one is read; the 429 stops the rest until next scan.
    assert [j.ats_id for j in jobs] == ids[:2]
    assert jobs[0].description == "" and jobs[1].description
    assert len(opened) == 2


# ---------------------------------------------------------------- Hirist, Instahyre, Unstop

def test_hirist_jobs():
    for raw in load("hirist_search.json")["data"]:
        job = ip.hirist_job(raw, src("hirist"))
        check(job)
        assert job.description.startswith("Skills: ") and job.experience_min == raw["min"]
        assert str(job.apply_url).startswith("https://www.hirist.tech/")


def test_instahyre_job_from_list_and_page(monkeypatch):
    data = load("instahyre_list.json")
    raw = data["objects"][0]
    from src.collect.jobpages import job_posting
    job = ip.instahyre_job(raw, src("instahyre"), job_posting(text("instahyre_job.html")))
    check(job)
    assert job.company == raw["employer"]["company_name"] and "Skills:" in job.description


def test_instahyre_opens_newest_first(monkeypatch):
    data, page = load("instahyre_list.json"), text("instahyre_job.html")
    opened = []
    monkeypatch.setattr(ip, "get_json", lambda url, params=None, headers=None: data)
    monkeypatch.setattr(ip, "get_text", lambda url, params=None, headers=None: opened.append(url) or page)
    monkeypatch.setattr(ip, "INSTAHYRE_MAX_NEW", 2)
    jobs = ip.fetch_instahyre(src("instahyre"))
    newest = sorted(data["objects"], key=lambda r: -r["id"])[:2]
    assert opened == [r["public_url"] for r in newest] and len(jobs) == 2


def test_unstop_jobs():
    for raw in load("unstop_jobs.json")["data"]["data"]:
        job = ip.unstop_job(raw, src("unstop"))
        check(job)
        assert str(job.apply_url).startswith("https://unstop.com/")


# ---------------------------------------------------------------- Foundit, Shine, TimesJobs

def test_foundit_jobs():
    for raw in load("foundit_search.json")["jobSearchResponse"]["data"]:
        job = ip.foundit_job(raw, src("foundit"))
        check(job)
        assert str(job.apply_url).startswith("https://www.foundit.in/job/")


def test_foundit_sends_referer(monkeypatch):
    seen = {}
    data = load("foundit_search.json")

    def fake_get(url, params=None, headers=None):
        seen.update(headers)
        return data

    monkeypatch.setattr(ip, "get_json", fake_get)
    assert ip.fetch_foundit(src("foundit", "developer"))
    assert seen["Referer"].startswith("https://www.foundit.in") and seen["Accept"] == "application/json"


def test_shine_jobs():
    for raw in load("shine_search.json")["results"]:
        job = ip.shine_job(raw, src("shine"))
        check(job)
        assert str(job.apply_url).startswith("https://www.shine.com/jobs/")


def test_timesjobs_jobs_have_clean_titles():
    for raw in load("timesjobs_list.json")["jobs"]:
        job = ip.timesjobs_job(raw, src("timesjobs"))
        check(job)
        assert "Job Details" not in job.title
    assert ip._TJ_TITLE_TAIL.sub("", "Java Developer Job Job Details | Yash Technologies") == "Java Developer"


# ---------------------------------------------------------------- AmbitionBox (Naukri)

def test_ambitionbox_jobs():
    raws = ip.next_data(text("ambitionbox_list.html"))["jobsList"]
    for raw in raws:
        job = ip.ambitionbox_job(raw, src("ambitionbox"))
        check(job)
        assert job.ats_id.startswith("naukri_") and str(job.apply_url).startswith("https://www.ambitionbox.com/jobs/")


def test_ambitionbox_skips_missing_lists_and_reports_the_bot_check(monkeypatch):
    page = text("ambitionbox_list.html")

    def fake_get(url, params=None, headers=None):
        if "missing" in url:
            raise SourceError("not_found", url)
        if "verify" in url:
            return "<title>Verify - AmbitionBox</title>"
        return page

    monkeypatch.setattr(ip, "get_text", fake_get)
    assert len(ip.fetch_ambitionbox(src("ambitionbox", "missing-jobs-ski|java-jobs-ski|verify-jobs-ski"))) == 3
    with pytest.raises(SourceError) as e:
        ip.fetch_ambitionbox(src("ambitionbox", "verify-jobs-ski"))
    assert e.value.reason == "blocked"


# ---------------------------------------------------------------- Wellfound, Internshala, The Muse

def test_wellfound_jobs_carry_their_company():
    jobs = ip.wellfound_jobs(text("wellfound_role.html"), src("wellfound"))
    assert jobs
    for job in jobs:
        assert job.title and job.company not in ("", "Unknown") and job.description
        assert str(job.apply_url).startswith("https://wellfound.com/jobs/")


def test_internshala_internships_and_jobs():
    for name, kind in (("internshala_internships.html", "Internship"), ("internshala_jobs.html", None)):
        cards = ip.internshala_cards(text(name))
        assert len(cards) == 3
        for card in cards:
            job = ip.internshala_job(card, src("internshala"))
            check(job)
            assert job.department == kind
            if kind:
                assert job.experience_min == 0 and (job.salary is None or "stipend" in job.salary)


def test_themuse_jobs_keep_their_real_place():
    jobs = [ip.themuse_job(raw, src("themuse")) for raw in load("themuse_jobs.json")["results"]]
    for job in jobs:
        assert job.title and job.company and job.description and job.posted_at
    # The place filter also returns US jobs; they keep their place so the rules can drop them.
    assert {j.india_eligible for j in jobs} >= {"yes"} and any(j.india_eligible == "no" for j in jobs)


# ---------------------------------------------------------------- Cutshort (sitemap + job pages)

def test_cutshort_reads_only_tech_job_pages(monkeypatch):
    urls = ["https://cutshort.io/job/Java-developer-Mumbai-Wissen-Technology-qHUo8b1i",
            "https://cutshort.io/job/Academic-Counselor-Bengaluru-Hello-Mentor-ZFl3GeXN",
            "https://cutshort.io/company/wissen"]
    assert jobpages.cutshort_pages(urls) == [("qHUo8b1i", urls[0])]
    monkeypatch.setattr(jobpages, "sitemap_urls", lambda board, headers=None: urls)
    monkeypatch.setattr(jobpages, "get_text", lambda url, params=None, headers=None: text("cutshort_job.html"))
    [job] = jobpages.fetch_cutshort(Source(name="Cutshort", type="cutshort", board=""))
    check(job)
    assert job.company == "Wissen Technology" and job.cities == ["Mumbai"]


def test_cutshort_remote_job_without_place_is_remote_india(monkeypatch):
    page = text("cutshort_job.html").replace('"jobLocation"', '"otherLocation"')
    monkeypatch.setattr(jobpages, "sitemap_urls", lambda board, headers=None:
                        ["https://cutshort.io/job/Cloud-DevOps-Engineer-Remote-Data-Fidelity-FN1NmZGi"])
    monkeypatch.setattr(jobpages, "get_text", lambda url, params=None, headers=None: page)
    [job] = jobpages.fetch_cutshort(Source(name="Cutshort", type="cutshort", board=""))
    assert job.location_raw == "Remote, India" and job.india_eligible == "yes" and job.work_mode == "remote"


# ---------------------------------------------------------------- Freshersworld (list pages + job pages)

def test_freshersworld_reads_list_pages_then_new_job_pages(monkeypatch):
    listing, page = text("freshersworld_list.html"), text("freshersworld_job.html")
    urls = jobpages.item_list(listing)
    assert len(urls) == 20
    calls = []

    def fake_get(url, params=None, headers=None):
        calls.append((url, params))
        return listing if "/category/" in url else page

    monkeypatch.setattr(jobpages, "get_text", fake_get)
    monkeypatch.setattr(jobpages, "MAX_NEW", 2)
    known = frozenset(jobpages._FW_ID_RE.search(u).group(1) for u in urls[5:])
    jobs = jobpages.fetch_freshersworld(Source(name="Freshersworld", type="freshersworld", board=""), known)
    # 15 of the 20 listed jobs are known (returned bare); only MAX_NEW of the 5 new ones are opened.
    opened = [u for u, _ in calls if "/category/" not in u]
    assert len(opened) == 2 and len(jobs) == 15 + 2
    for job in jobs:
        if job.description:
            check(job)
