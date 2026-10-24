"""Jooble and Careerjet (job portals with an API key) parse saved real responses."""

import json
from pathlib import Path

import pytest

from src import digest
from src.collect import portals
from src.collect.http import SourceError
from src.models import Source

FIX = Path(__file__).parent / "fixtures"
JOOBLE = Source(name="Jooble", type="jooble", board="developer|software engineer")
CAREERJET = Source(name="Careerjet", type="careerjet", board="software engineer|developer")


def load(name):
    return json.loads((FIX / name).read_text())


def check(job):
    assert job.title and job.ats_id and job.company != "Unknown"
    assert str(job.apply_url).startswith("https://")
    assert job.posted_at is not None
    assert len(job.description) > 50 and "<b>" not in job.description and "&nbsp;" not in job.description
    assert job.india_eligible == "yes"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "test-key")
    monkeypatch.setenv("CAREERJET_API_KEY", "test-key")
    monkeypatch.setattr(portals, "PAGE_DELAY", 0)
    monkeypatch.setattr(portals, "MAX_AGE_DAYS", 100000)


def test_jooble_jobs_are_india_jobs_with_board_link():
    for raw in load("jooble_india.json")["jobs"]:
        job = portals.jooble_job(raw, JOOBLE)
        check(job)
        assert job.location_raw == "India" and str(job.apply_url).startswith("https://jooble.org/jdp/")


def test_jooble_one_request_per_keyword_when_page_not_full(monkeypatch):
    calls = []
    data = load("jooble_india.json")

    def fake_post(url, body):
        calls.append((url, body))
        return data

    monkeypatch.setattr(portals, "post_json", fake_post)
    jobs = portals.fetch_jooble(JOOBLE)
    assert len(calls) == 2 and calls[0][0] == "https://jooble.org/api/test-key"
    assert [b["keywords"] for _, b in calls] == ["developer", "software engineer"]
    assert len(jobs) == len(data["jobs"])   # the same jobs from both keywords are kept once


def test_careerjet_city_gets_india_and_id_is_stable():
    raw = load("careerjet_india.json")["jobs"]
    job = portals.careerjet_job(raw[0], CAREERJET)
    check(job)
    assert job.location_raw == "Hyderabad, Telangana" and job.cities == ["Hyderabad"]
    # The tracking URL changes on every call; the ID must not.
    assert portals.careerjet_id({**raw[0], "url": "https://jobviewtrack.com/other"}) == job.ats_id
    assert len({portals.careerjet_id(r) for r in raw}) == len(raw)


def test_careerjet_sends_referer_and_stops_when_caught_up(monkeypatch):
    data = load("careerjet_india.json")
    data["jobs"] = data["jobs"] * 5          # a full page of 50
    calls = []

    def fake_get(url, params=None, headers=None):
        calls.append((params, headers))
        return data

    monkeypatch.setattr(portals, "get_json", fake_get)
    known = frozenset(portals.careerjet_id(r) for r in data["jobs"])
    portals.fetch_careerjet(CAREERJET, known)
    assert len(calls) == 2                   # one page per keyword: everything already known
    params, headers = calls[0]
    assert params["affid"] == "test-key" and params["locale_code"] == "en_IN" and headers["Referer"]


def test_careerjet_error_reply_is_a_source_failure(monkeypatch):
    monkeypatch.setattr(portals, "get_json", lambda url, params=None, headers=None:
                        {"type": "ERROR", "error": "Undeclared referrer"})
    with pytest.raises(SourceError) as e:
        portals.fetch_careerjet(CAREERJET)
    assert e.value.reason == "blocked"


def test_missing_key_is_a_clear_failure(monkeypatch):
    monkeypatch.delenv("JOOBLE_API_KEY")
    with pytest.raises(SourceError) as e:
        portals.fetch_jooble(JOOBLE)
    assert e.value.reason == "no_api_key"


def test_apply_links_are_kept_on_portal_hosts():
    assert digest.safe_apply_url("https://jooble.org/jdp/123", "jooble", "", "123", set()) \
        == "https://jooble.org/jdp/123"
    assert digest.safe_apply_url("https://jobviewtrack.com/v2/abc", "careerjet", "", "x", set()) \
        == "https://jobviewtrack.com/v2/abc"
    assert digest.safe_apply_url("http://evil.example/x", "jooble", "", "123", set()) \
        == "https://jooble.org/jdp/123"
