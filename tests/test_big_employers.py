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


def test_oracle_job_and_india_location_id():
    f = load("oracle_jpmc.json")
    job = oracle.to_job(f["item"], f["detail"], JPM)
    check(job)
    assert str(job.apply_url) == f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/{f['item']['Id']}"
    assert oracle.india_location_id(f["locationsFacet"]) == 300000000289360
    assert oracle.india_location_id([{"Name": "United States", "Id": 1}]) is None


def test_known_oracle_job_has_no_description():
    f = load("oracle_jpmc.json")
    job = oracle.to_job(f["item"], None, JPM)
    assert job.description == "" and job.india_eligible == "yes"


def test_oracle_site_parsing():
    assert oracle.site(JPM.board) == ("jpmc.fa.oraclecloud.com", "CX_1001")
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
