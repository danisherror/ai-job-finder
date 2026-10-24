"""Hard requirements checked in code, before any AI call. Pure functions."""

import re
from datetime import datetime, timezone

from src.models import Job, Profile

LEVELS = ["intern", "junior", "mid", "senior", "lead"]

# Checked in this order; the first match decides. A title with none of these is "unknown".
_LEVEL_RE = [
    ("intern", re.compile(r"\b(intern|internship|trainee|apprentice)\b", re.I)),
    ("lead", re.compile(r"\b(lead|staff|principal|head|director|vp|vice president|chief|distinguished)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|sde[- ]?(iii|3)|engineer (iii|3))\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|new grad|fresher|associate|"
                          r"sde[- ]?(i|1)|engineer (i|1))\b", re.I)),
    ("mid", re.compile(r"\b(sde[- ]?(ii|2)|engineer (ii|2)|mid[- ]level)\b", re.I)),
]


def title_level(title: str) -> str | None:
    for level, rx in _LEVEL_RE:
        if rx.search(title):
            return level
    return None


# ---------------------------------------------------------------- software roles

_SOFTWARE = re.compile(
    r"\b(software|sw|sde\w*|developer|development engineer|programmer|coding|full[- ]?stack|front[- ]?end|"
    r"back[- ]?end|web|android|ios|mobile|python|java|ruby|rails|golang|node(\.?js)?|react|devops|sre|"
    r"site reliability|cloud|azure|aws|data scien\w*|data engineer\w*|big data|machine learning|ml|ai|"
    r"artificial intelligence|llm|deep learning|computer vision|qa|quality assurance|sdet|test(ing)? automation|"
    r"automation|cyber ?security|security (engineer|research|consultant)\w*|game development|"
    r"forward deployed engineer)\b", re.I)
_NOT_SOFTWARE = re.compile(r"\b(hw|hardware|asic|fpga|silicon|technical support|recruit\w*|talent acquisition|"
                           r"sales|marketing)\b", re.I)
# A title with none of the software words and none of these is too vague ("Intern", "Campus Program - 2027"),
# so the description decides.
_OTHER_ROLE = re.compile(
    r"\b(analyst|account\w*|audit|finance|financial|hr|human resources|people|operations?|design\w*|content|"
    r"manager|management|executive|associate|specialist|representative|technician|chemical|industrial|patent|"
    r"banking|support|success|cfo|ca|kyc|advisory|procurement|product|ambassador|video|social|copy|brand|"
    r"animator|editor|writer|lead generation|business|public relations|system|systems|applications engineer|"
    r"quality|camera|wlan|network)\b", re.I)
_CODING_TERMS = re.compile(r"\b(software development|software engineering|programming|python|java|c\+\+|"
                           r"computer science|data structures|algorithms|coding|machine learning|full[- ]stack)\b",
                           re.I)


def is_software_role(title: str, description: str = "") -> bool:
    """Software, data/AI, QA, DevOps or security engineering.

    Judged from the title ("Intern_2027_SW" counts; "Engineer_HW", "IT Recruiter" don't). When the title is too
    vague to tell, the description must name at least three coding terms (Python, programming, ...).
    """
    t = title.replace("_", " ")
    if _NOT_SOFTWARE.search(t):
        return False
    if _SOFTWARE.search(t):
        return True
    if _OTHER_ROLE.search(t):
        return False
    return len({m.group(0).lower() for m in _CODING_TERMS.finditer(description)}) >= 3


# ---------------------------------------------------------------- graduating students

_BATCH_WORDS = re.compile(r"batch|graduat\w*|passing|pass[- ]?outs?|passed out|class of|campus|cohort|degree|"
                          r"students?|freshers?|final[- ]year|completion|\b[bm]\.?\s?tech\b|\bmca\b|\bb\.e\b", re.I)
_YEAR = re.compile(r"\b(202[3-9]|2030)(?:\s*[/-]\s*(\d\d)(?!\d))?\b")  # also "2025/26"
_DATE_BEFORE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s*\d{0,2},?\s*$|/\s*$", re.I)
_EARLY_TITLE = re.compile(r"\b(new grads?|graduates?|grads?|freshers?|campus|early[- ]careers?|early talent|"
                          r"entry[- ]level|university|college|trainee|apprentice\w*|fellow\w*)\b", re.I)
_RETURNER = re.compile(r"return to work|returnship|returner", re.I)


def batch_years(text: str) -> set[int]:
    """Graduation years a posting asks for: "2027 batch", "Passing year: 2025, 2026 or 2027", "2025/26".
    Years in dates ("Jan 2027", "02/27/2027") or plans ("by 2027") are not counted."""
    years = set()
    for m in _YEAR.finditer(text):
        near = text[max(0, m.start() - 60):m.end() + 40]
        if _BATCH_WORDS.search(near) and not _DATE_BEFORE.search(text[max(0, m.start() - 12):m.start()]):
            years.add(int(m.group(1)))
            if m.group(2):
                years.add(2000 + int(m.group(2)))
    return years


def graduate_fit(title: str, description: str, experience_min: int | None, experience_max: int | None,
                 year: int) -> str | None:
    """Why a student graduating in `year` can apply, or None.

    The year in the title, or named as a batch in the text -> "<year> batch"
    The text names other batches only                   -> None
    Internship, campus/graduate/fresher title, or 0 years needed -> that reason
    """
    if _RETURNER.search(title):  # for people coming back after a career break
        return None
    if str(year) in title:
        return f"{year} batch"
    level = title_level(title)
    if level in ("senior", "lead") or (experience_min or 0) >= 1:
        return None
    years = batch_years(f"{title}\n{description}")
    if year in years:
        return f"{year} batch"
    if years:
        return None
    if level == "intern":
        return "internship"
    if _EARLY_TITLE.search(title):
        return "campus / graduate / fresher role"
    if experience_min == 0 and (experience_max is None or experience_max <= 2):
        return "fresher role (0 years)"
    return None


def allowed_levels(profile: Profile) -> set[str]:
    i = LEVELS.index(profile.seniority)
    allowed = {profile.seniority}
    if profile.stretch_up and i + 1 < len(LEVELS):
        allowed.add(LEVELS[i + 1])
    return allowed


def _contains(text: str, keyword: str) -> bool:
    # Word boundaries where the keyword starts/ends with a letter or digit, so "Go" doesn't match "Google"
    # but "10+ years" and "C++" still work.
    k = re.escape(keyword.strip())
    if re.match(r"\w", keyword.strip()):
        k = r"\b" + k
    if re.search(r"\w$", keyword.strip()):
        k = k + r"\b"
    return re.search(k, text, re.I) is not None


_RUPEES_RE = re.compile(r"INR|₹|\bRs\b|\bLPA\b", re.I)
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(lpa|lakhs?|lacs?|l|crores?|cr|k)?\b(?![\d%.])", re.I)
_UNIT_LPA = {"lpa": 1, "l": 1, "lakh": 1, "lakhs": 1, "lac": 1, "lacs": 1,
             "cr": 100, "crore": 100, "crores": 100, "k": 0.01}


def salary_max_lpa(salary: str | None) -> float | None:
    """Top of an Indian yearly salary range in lakhs per annum ("INR 1200000-2000000", "₹40L – ₹1Cr").
    None if the text isn't clearly an INR yearly amount."""
    if not salary or not _RUPEES_RE.search(salary) or re.search(r"\b(hour|month|week|day)", salary, re.I):
        return None
    amounts = []
    for number, unit in _AMOUNT_RE.findall(salary):
        value = float(number.replace(",", ""))
        if unit:
            amounts.append(value * _UNIT_LPA[unit.lower()])
        elif value >= 100_000:          # plain rupees
            amounts.append(value / 100_000)
    return max(amounts[:2]) if amounts else None


def passes_rules(job: Job, profile: Profile, now: datetime | None = None) -> tuple[bool, str]:
    """(True, "ok") or (False, short reason). Unknown values pass; the AI or the person judges those."""
    now = now or datetime.now(timezone.utc)

    # India eligibility and place
    if job.india_eligible == "no":
        return False, "not open to India"
    remote_fits = job.work_mode == "remote" and profile.remote_ok and "remote" in profile.work_modes
    city_fits = bool(set(job.cities) & set(profile.locations))
    if not remote_fits:
        if not city_fits:
            return False, "remote, person wants office" if job.work_mode == "remote" else "location"
        if job.work_mode in ("hybrid", "onsite") and job.work_mode not in profile.work_modes:
            return False, f"work mode {job.work_mode}"

    # Experience: allow asking up to 1 year more than they have; drop jobs far below their level
    years = profile.experience_years
    if job.experience_min is not None and job.experience_min > years + 1:
        return False, f"needs {job.experience_min}+ years"
    if job.experience_max is not None and job.experience_max + 2 < years:
        return False, f"for up to {job.experience_max} years"

    # Seniority from the title
    level = title_level(job.title)
    if level is not None and level not in allowed_levels(profile):
        return False, f"seniority {level}"

    # Keywords
    for k in profile.exclude_title_keywords:
        if _contains(job.title, k):
            return False, f"title has '{k}'"
    text = f"{job.title}\n{job.description}"
    for k in profile.exclude_keywords:
        if _contains(text, k):
            return False, f"has '{k}'"
    if profile.require_keywords and not any(_contains(text, k) for k in profile.require_keywords):
        return False, "no required keyword"

    # Salary, only when the job states an INR yearly range
    top = salary_max_lpa(job.salary)
    if profile.min_salary_lpa and top is not None and top < profile.min_salary_lpa:
        return False, f"salary up to {top:g} LPA"

    # Posting age
    posted = job.posted_at or job.updated_at
    if posted is not None:
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if (now - posted).days > profile.max_age_days:
            return False, "too old"

    return True, "ok"
