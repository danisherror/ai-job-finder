"""Hard rules: place, experience, seniority, keywords, salary, age."""

from datetime import datetime, timedelta, timezone

import pytest

from src.models import Job, Profile
from src.rules import passes_rules, title_level

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def profile(**kw) -> Profile:
    base = dict(id="p", name="P", target_roles=["Backend Engineer"], seniority="mid", experience_years=3,
                locations=["Bengaluru"], remote_ok=True)
    return Profile(**(base | kw))


def job(**kw) -> Job:
    base = dict(source="X", ats_id="1", title="Backend Engineer", company="X", location_raw="Bengaluru, India",
                cities=["Bengaluru"], work_mode="onsite", india_eligible="yes", description="We use Python.",
                apply_url="https://jobs.lever.co/x/1", posted_at=NOW - timedelta(days=2))
    return Job(**(base | kw))


def check(j, p=None):
    return passes_rules(j, p or profile(), NOW)


def test_plain_match_passes():
    assert check(job()) == (True, "ok")


@pytest.mark.parametrize("j, p, reason", [
    (job(india_eligible="no", work_mode="remote", cities=[]), None, "not open to India"),
    (job(cities=["Pune"]), None, "location"),
    (job(work_mode="remote", cities=[]), profile(remote_ok=False), "remote, person wants office"),
    (job(work_mode="hybrid"), profile(work_modes=["onsite"]), "work mode hybrid"),
    (job(experience_min=5), None, "needs 5+ years"),
    (job(experience_min=0, experience_max=0), profile(experience_years=6), "for up to 0 years"),
    (job(title="Senior Backend Engineer"), None, "seniority senior"),
    (job(title="Staff Engineer"), profile(stretch_up=True), "seniority lead"),
    (job(title="Backend Engineer Intern"), None, "seniority intern"),
    (job(title="Account Executive"), profile(exclude_title_keywords=["Account Executive"]),
     "title has 'Account Executive'"),
    (job(description="Requires 10+ years"), profile(exclude_keywords=["10+ years"]), "has '10+ years'"),
    (job(), profile(require_keywords=["Golang"]), "no required keyword"),
    (job(salary="INR 600000-900000 per year"), profile(min_salary_lpa=12), "salary up to 9 LPA"),
    (job(posted_at=NOW - timedelta(days=45)), None, "too old"),
])
def test_rejections(j, p, reason):
    assert check(j, p) == (False, reason)


@pytest.mark.parametrize("j, p", [
    (job(work_mode="remote", cities=[], india_eligible="unclear"), None),     # unclear passes; the AI flags it
    (job(work_mode="remote", cities=["Bengaluru"]), profile(remote_ok=False)),  # remote but in their city
    (job(experience_min=4), None),                                            # one year of stretch
    (job(title="Senior Backend Engineer"), profile(stretch_up=True)),
    (job(salary="₹40L – ₹1Cr"), profile(min_salary_lpa=12)),
    (job(salary="USD 100000-150000"), profile(min_salary_lpa=200)),           # not INR: unknown, passes
    (job(description="Experience with Google Cloud"), profile(exclude_keywords=["Go"])),  # word boundary
])
def test_passes(j, p):
    assert check(j, p) == (True, "ok")


@pytest.mark.parametrize("title, level", [
    ("Software Engineer", None),
    ("Senior Software Engineer", "senior"),
    ("Sr. Backend Developer", "senior"),
    ("SDE II", "mid"),
    ("SDE-1", "junior"),
    ("Associate Software Engineer", "junior"),
    ("Engineering Lead", "lead"),
    ("Principal Engineer", "lead"),
    ("Product Manager", None),
    ("Software Engineering Intern", "intern"),
])
def test_title_level(title, level):
    assert title_level(title) == level
