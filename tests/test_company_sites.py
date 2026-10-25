"""Big employers' own career sites (Google, Apple, IBM, Goldman Sachs, SuccessFactors sites, MakeMyTrip, Uber's
sitemap) parse saved real responses."""

import json
from pathlib import Path

import pytest

from src.collect import careersites as cs
from src.collect import jobpages
from src.models import Source

FIX = Path(__file__).parent / "fixtures"


def text(name):
    return (FIX / name).read_text()


def load(name):
    return json.loads(text(name))


def check(job, description=True):
    assert job.title and job.ats_id and job.company
    assert str(job.apply_url).startswith("https://")
    assert job.india_eligible == "yes", job.location_raw
    if description:
        assert len(job.description) > 100 and "<div>" not in job.description and "&lt;" not in job.description


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(cs, "PAGE_DELAY", 0)
    monkeypatch.setattr(jobpages, "DELAY", 0)


def test_google_jobs_carry_the_hiring_company():
    raws, total = cs.google_page(text("google_search.html"))
    assert len(raws) == total == 2
    for raw in raws:
        job = cs.google_job(raw, Source(name="Google", type="google", board="India"))
        check(job)
        assert job.posted_at and job.company in ("Google", "DeepMind", "YouTube", "Waymo")
        assert str(job.apply_url) == f"https://www.google.com/about/careers/applications/jobs/results/{raw[0]}"


def test_google_stops_at_the_last_page(monkeypatch):
    calls = []
    page = text("google_search.html")
    monkeypatch.setattr(cs, "get_text", lambda url, headers=None, params=None: calls.append(params) or page)
    jobs = cs.fetch_google(Source(name="Google", type="google", board="India"))
    assert len(jobs) == 2 and len(calls) == 1 and calls[0]["sort_by"] == "date"


def test_apple_jobs():
    raws, total = cs.apple_page(text("apple_search.html"))
    assert len(raws) == total == 2
    for raw in raws:
        job = cs.apple_job(raw, Source(name="Apple", type="apple", board="india-INDC"))
        check(job, description=False)
        assert job.company == "Apple" and job.posted_at and job.description
        assert str(job.apply_url).startswith("https://jobs.apple.com/en-in/details/")


def test_ibm_jobs():
    for hit in load("ibm_search.json")["hits"]["hits"]:
        job = cs.ibm_job(hit, Source(name="IBM", type="ibm", board="India"))
        check(job, description=False)
        assert job.posted_at and job.location_raw.endswith("India") and job.cities
        assert str(job.apply_url).startswith("https://careers.ibm.com/careers/JobDetail?jobId=")


def test_ibm_place_always_names_the_country():
    hit = {"_id": "x", "_source": {"title": "Data Engineer", "url": "https://careers.ibm.com/careers/JobDetail?jobId=1",
                                   "field_keyword_05": "India", "field_keyword_19": "Multiple Cities"}}
    job = cs.ibm_job(hit, Source(name="IBM", type="ibm", board="India"))
    assert job.location_raw == "Multiple Cities, India" and job.india_eligible == "yes"
    hit["_source"]["field_keyword_19"] = "GURGAON, IN"
    assert cs.ibm_job(hit, Source(name="IBM", type="ibm", board="India")).location_raw == "Gurgaon, India"


def test_goldman_jobs_have_full_descriptions():
    items = load("goldman_roles.json")["data"]["roleSearch"]["items"]
    for raw in items:
        job = cs.goldman_job(raw, Source(name="Goldman Sachs", type="goldman", board="India"))
        check(job)
        assert str(job.apply_url).startswith("https://higher.gs.com/roles/")


def test_sapsf_list_and_job_pages():
    raws = [r["response"] for r in load("sapsf_wipro_jobs.json")["jobSearchResult"]]
    base = "https://careers.wipro.com"
    wipro = Source(name="Wipro", type="sapsf", board=f"{base}?location=India")
    job = cs.sapsf_job(raws[0], base, wipro, cs.sapsf_description(text("sapsf_wipro_job.html")))
    check(job)
    assert job.posted_at and job.cities
    assert str(job.apply_url) == f"{base}/job/{raws[0]['urlTitle']}/{raws[0]['id']}-en_US/"
    # HCLTech's list gives the city and country in other fields; its page uses the same microdata.
    hcl = {"id": "135830", "unifiedStandardTitle": "Senior Developer", "urlTitle": "Senior-Developer",
           "custprimecity": "Chennai", "custCountryRegion": ["India"], "unifiedStandardStart": "10/25/26"}
    job = cs.sapsf_job(hcl, "https://careers.hcltech.com", Source(name="HCLTech", type="sapsf", board="x"),
                       cs.sapsf_description(text("sapsf_hcl_job.html")))
    check(job)
    assert job.location_raw == "Chennai, India" and job.posted_at.year == 2026


def test_sapsf_reads_filters_from_the_board_and_opens_only_new_jobs(monkeypatch):
    raws = [r["response"] for r in load("sapsf_wipro_jobs.json")["jobSearchResult"]]

    class FakeSite:
        def __init__(self, board):
            self.base = "https://careers.hcltech.com"
            assert board.endswith("?custCountryRegion=India")

        def jobs(self, page):
            return (raws if page == 0 else []), len(raws)

    opened = []
    monkeypatch.setattr(cs, "_Site", FakeSite)
    monkeypatch.setattr(cs, "get_text", lambda url, headers=None: opened.append(url) or text("sapsf_hcl_job.html"))
    jobs = cs.fetch_sapsf(Source(name="HCLTech", type="sapsf", board="https://careers.hcltech.com?custCountryRegion=India"),
                          known=frozenset({raws[0]["id"]}))
    assert len(jobs) == len(raws) and len(opened) == len(raws) - 1
    assert jobs[0].description == "" and all(j.description for j in jobs[1:])


def test_sapsf_board_holds_the_site_and_its_india_filter():
    assert cs.sapsf_board("https://careers.wipro.com?location=India") == ("https://careers.wipro.com", "India", {})
    assert cs.sapsf_board("careers.hcltech.com?custCountryRegion=India") == \
        ("https://careers.hcltech.com", "", {"custCountryRegion": ["India"]})


def test_makemytrip_jobs():
    raws = load("mmt_jobs.json")["allJobs"]
    detail = load("mmt_detail.json")["data"]
    job = cs.mmt_job(raws[0], Source(name="MakeMyTrip", type="makemytrip", board=""), detail)
    check(job)
    assert job.company.startswith("MakeMyTrip") and job.posted_at and "(" not in job.location_raw
    assert str(job.apply_url).startswith(f"https://careers.makemytrip.com/opportunity/{raws[0]['job_id']}/")


def test_sitemap_reader_opens_numbered_job_pages(monkeypatch):
    urls = ["https://jobs.uber.com/en/jobs/149574/", "https://jobs.uber.com/en/jobs/", "https://jobs.uber.com/en/about/"]
    assert jobpages.numbered_pages(urls) == [("149574", urls[0])]
    monkeypatch.setattr(jobpages, "sitemap_urls", lambda board, headers=None: urls)
    monkeypatch.setattr(jobpages, "get_text", lambda url, params=None, headers=None: text("uber_job.html"))
    [job] = jobpages.fetch_sitemap(Source(name="Uber", type="sitemap", board="https://jobs.uber.com/en/jobs/sitemap.xml"))
    assert job.company == "Uber" and job.title and job.description and job.posted_at
