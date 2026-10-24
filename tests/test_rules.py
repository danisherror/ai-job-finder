"""Hard rules: place, experience, seniority, keywords, salary, age."""

from datetime import datetime, timedelta, timezone

import pytest

from src.models import Job, Profile
from src.rules import batch_years, graduate_fit, is_software_role, passes_rules, title_level

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


# ---------------------------------------------------------------- graduating students

def test_batch_years_reads_graduation_years_not_dates():
    assert batch_years("Passing year: 2025, 2026 or 2027 (preferred).") == {2025, 2026, 2027}
    assert batch_years("Open to 2026 - 2027 graduates") == {2026, 2027}
    assert batch_years("The program will run from January 2027.") == set()
    assert batch_years("Window closes on 02/27/2027") == set()
    assert batch_years("By 2027, we aim to be the top destination.") == set()


@pytest.mark.parametrize("title, text, exp_min, exp_max, why", [
    ("2027 Campus Hire_Engineer_SW", "", None, None, "2027 batch"),
    ("Business Development Associate", "Passing year: 2026 or 2027", None, None, "2027 batch"),
    ("Apprentice", "B.Tech (2023 & 2024 graduates only)", None, None, None),  # other batches only
    ("Software Engineer Intern", "6-month internship, Jan - June 2027", None, None, "internship"),
    ("Graduate Programme - India", "", None, None, "campus / graduate / fresher role"),
    ("SDE I", "", 0, 1, "fresher role (0 years)"),
    ("Software Engineer", "", 2, 4, None),
    ("Software Engineer", "", None, None, None),  # nothing says it suits students
    ("Senior Engineer", "", 0, 2, None),
    ("Return to Work Program - Mumbai", "graduates welcome", None, None, None),
])
def test_graduate_fit(title, text, exp_min, exp_max, why):
    assert graduate_fit(title, text, exp_min, exp_max, 2027) == why


@pytest.mark.parametrize("title, text, expected", [
    ("Software Development Engineer Intern", "", True),
    ("Interim Engineering Intern_2027_SW", "", True),
    ("Machine Learning (ML) Engineer Intern", "", True),
    ("QA Engineer", "", True),
    ("2027 Campus Hire_ Engineer_ HW", "", False),
    ("Talent Acquisition Intern - IT Recruitment", "", False),
    ("Technical Support Engineer", "", False),
    ("Marketing Intern", "Python, programming and computer science", False),  # title decides
    ("Intern", "You know Python, Java and programming basics.", True),            # vague: text decides
    ("Intern", "Support the finance team with month-end close.", False),
])
def test_is_software_role(title, text, expected):
    assert is_software_role(title, text) is expected


def test_batch_years_reads_short_year_ranges():
    assert batch_years("Engineer - RF SW - M.Tech - Communication background - 2025/26") == {2025, 2026}
