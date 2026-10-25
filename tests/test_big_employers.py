"""Amazon, Oracle Cloud HCM, Eightfold and Rippling adapters parse saved real feeds."""

import json
from pathlib import Path

import pytest

from src import digest
from src import normalize as n
from src.collect import amazon, eightfold, oracle, rippling
from src.collect.http import SourceError
from src.models import Job, Source

FIX = Path(__file__).parent / "fixtures"
AMZ = Source(name="Amazon", type="amazon", board="IND")
JPM = Source(name="JPMorgan", type="oracle", board="https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001")
MS = Source(name="Microsoft", type="eightfold", board="https://apply.careers.microsoft.com/careers?domain=microsoft.com")
RIP = Source(name="Rippling", type="rippling", board="rippling")


def load(name):
    return json.loads((FIX / name).read_text())


def check(job: Job):
    assert job.title and job.ats_id and job.location_raw
    assert str(job.apply_url).startswith("https://")
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert len(job.description) > 100 and "<p>" not in job.description and "<br" not in job.description
    assert job.india_eligible == "yes" and job.cities


def test_amazon_job_has_qualifications_and_india_location():
    raw = load("amazon_india.json")["jobs"][0]
    job = amazon.to_job(raw, AMZ)
    check(job)
    assert "Basic qualifications" in job.description
    assert job.location_raw.endswith(", India")
    assert str(job.apply_url).startswith("https://www.amazon.jobs/en/jobs/")


def test_amazon_keeps_only_tech_categories():
    cats = [r["job_category"] for r in load("amazon_india.json")["jobs"]]
    assert cats[0] in amazon.TECH_CATEGORIES and cats[1] not in amazon.TECH_CATEGORIES


def test_oracle_job():
    f = load("oracle_jpmc.json")
    job = oracle.to_job(f["item"], f["detail"], JPM)
    check(job)
    assert str(job.apply_url) == f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/{f['item']['Id']}"


def test_known_oracle_job_has_no_description():
    f = load("oracle_jpmc.json")
    job = oracle.to_job(f["item"], None, JPM)
    assert job.description == "" and job.india_eligible == "yes"


def test_oracle_site_parsing():
    assert oracle.site(JPM.board) == ("jpmc.fa.oraclecloud.com", "CX_1001")
    dell = "https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/careers"
    assert oracle.site(dell) == ("enterpriseplatform.dell.com", "careers")
    with pytest.raises(SourceError):
        oracle.site("https://example.com/careers")


def test_eightfold_job_reads_hybrid_from_office_days():
    f = load("eightfold_microsoft.json")
    job = eightfold.to_job(f["item"], f["detail"], MS)
    check(job)
    assert job.work_mode == "hybrid"  # "3 days / week in-office"
    assert job.department == "Software Engineering"


def test_eightfold_site_needs_domain():
    assert eightfold.site(MS.board) == ("https://apply.careers.microsoft.com", "microsoft.com")
    with pytest.raises(SourceError):
        eightfold.site("https://qualcomm.eightfold.ai/careers")


def test_rippling_job_skips_company_blurb():
    f = load("rippling_rippling.json")
    job = rippling.to_job(f["list"][0], f["detail"], RIP)
    check(job)
    company_blurb = n.html_to_text(f["detail"]["description"]["company"])[:60]
    assert len(company_blurb) > 30
    assert company_blurb not in job.description


@pytest.mark.parametrize("source_type,board,ats_id,expected", [
    ("amazon", "IND", "10588103", "https://www.amazon.jobs/en/jobs/10588103"),
    ("oracle", JPM.board, "210729983", JPM.board + "/job/210729983"),
    ("eightfold", MS.board, "1970", "https://apply.careers.microsoft.com/careers/job/1970"),
    ("rippling", "rippling", "ab-12", "https://ats.rippling.com/rippling/jobs/ab-12"),
])
def test_unsafe_apply_links_fall_back_to_the_job_page(source_type, board, ats_id, expected):
    assert digest.safe_apply_url("http://evil.example/x", source_type, board, ats_id, set()) == expected


def test_apply_links_on_the_system_are_kept():
    url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1"
    assert digest.safe_apply_url(url, "oracle", JPM.board, "1", set()) == url
    url = "https://careers.qualcomm.com/careers/job/44"
    assert digest.safe_apply_url(url, "eightfold", "https://qualcomm.eightfold.ai/careers?domain=qualcomm.com",
                                 "44", {"careers.qualcomm.com"}) == url


def test_redirect_to_a_maintenance_page_is_reported_as_maintenance(monkeypatch):
    import httpx

    from src.collect import http

    def fake(method, url, **kw):
        return httpx.Response(200, text="<html>down</html>",
                              request=httpx.Request("GET", "https://community.workday.com/maintenance-page"))
    monkeypatch.setattr(http.httpx, "request", fake)
    with pytest.raises(SourceError) as err:
        http.get_json("https://nvidia.wd5.myworkdayjobs.com/x")
    assert err.value.reason == "maintenance"



def test_job_url_with_maintenance_in_its_title_is_not_a_maintenance_page(monkeypatch):
    import httpx

    from src.collect import http

    url = "https://valeo.wd3.myworkdayjobs.com/wday/cxs/valeo/valeo_jobs/job/Chennai/Maintenance-Technician_REQ1"

    def fake(method, u, **kw):
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, u))
    monkeypatch.setattr(http.httpx, "request", fake)
    assert http.get_json(url) == {"ok": True}

# ---------------------------------------------------------------- job boards: We Work Remotely, YC

def test_weworkremotely_feed():
    from src.collect import weworkremotely
    src = Source(name="We Work Remotely", type="weworkremotely", board="full-stack-programming")
    items = weworkremotely.parse_feed((FIX / "weworkremotely_fullstack.rss").read_text())
    worldwide, regional = (weworkremotely.to_job(i, src) for i in items)
    assert worldwide.location_raw == "Remote - Worldwide" and worldwide.work_mode == "remote"
    assert worldwide.india_eligible in ("yes", "unclear")
    assert worldwide.company and ":" not in worldwide.title
    assert str(worldwide.apply_url).startswith("https://weworkremotely.com/remote-jobs/")
    assert len(worldwide.description) > 100 and "<p>" not in worldwide.description
    assert worldwide.posted_at.tzinfo is not None
    assert regional.india_eligible == "no"  # e.g. "USA Only"


def test_weworkremotely_rejects_non_rss():
    from src.collect import weworkremotely
    with pytest.raises(SourceError):
        weworkremotely.parse_feed("<html>oops")


def test_ycombinator_job_reads_country_codes():
    from src.collect import ycombinator
    src = Source(name="YC Jobs", type="ycombinator", board="india")
    f = load("ycombinator_india.json")
    job = ycombinator.to_job(f["postings"][0], f["detail"], src)
    assert "India" in job.location_raw and job.india_eligible == "yes"   # "IN" means India on YC
    assert job.company != "YC Jobs" and len(job.description) > 100
    assert str(job.apply_url).startswith("https://www.ycombinator.com/companies/")
    us = f["postings"][1]
    assert ycombinator.location_of(us) == "Remote (US)"
    assert n.india_eligibility(ycombinator.location_of(us), "remote") == "no"   # skipped without a page call


def test_ycombinator_age_to_date():
    from datetime import datetime, timezone

    from src.collect import ycombinator
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert ycombinator.posted_at("2 days", now).day == 22
    assert ycombinator.posted_at("about 1 month", now).month == 9
    assert ycombinator.posted_at(None) is None


def test_ycombinator_page_without_data_fails_clearly():
    from src.collect import ycombinator
    with pytest.raises(SourceError):
        ycombinator.page_props("<html></html>")


def test_rate_limits_wait_longer_then_succeed(monkeypatch):
    import httpx

    from src.collect import http
    waits, replies = [], iter([429, 429, 200])

    def fake(method, url, **kw):
        code = next(replies)
        return httpx.Response(code, json={"ok": True}, headers={"Retry-After": "7"} if code == 429 else {},
                              request=httpx.Request(method, url))
    monkeypatch.setattr(http.httpx, "request", fake)
    monkeypatch.setattr(http.time, "sleep", waits.append)
    assert http.get_json("https://example.com/x") == {"ok": True}
    assert waits == [7, 7]


def test_rate_limit_that_never_lifts_is_reported(monkeypatch):
    import httpx

    from src.collect import http
    monkeypatch.setattr(http.httpx, "request", lambda m, u, **kw: httpx.Response(429, request=httpx.Request(m, u)))
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    with pytest.raises(SourceError) as err:
        http.get_json("https://example.com/x")
    assert err.value.reason == "rate_limited"
