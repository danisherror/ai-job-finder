"""A plain keyword-overlap ranker. The AI matcher has to beat this on labelled jobs to earn its place."""

import re

from src.models import Job, Profile
from src.rules import _contains

# Words too common in job titles to say anything about fit on their own
_FILLER = {"engineer", "developer", "senior", "junior", "lead", "staff", "principal", "i", "ii", "iii", "and", "of",
           "the", "-", "/", "&"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]+", text.lower()) if w not in _FILLER}


def title_score(title: str, roles: list[str]) -> float:
    """1.0 if a target role appears in the title, else the best share of a role's key words found in it."""
    best = 0.0
    title_words = _words(title)
    for role in roles:
        if _contains(title, role):
            return 1.0
        words = _words(role)
        if words:
            best = max(best, len(words & title_words) / len(words))
    return best


def score(job: Job, profile: Profile) -> float:
    """Higher is better. Title fit counts most, then strong skills, then other skills."""
    text = f"{job.title}\n{job.description}"
    strong = sum(_contains(text, s) for s in profile.skills_strong)
    some = sum(_contains(text, s) for s in profile.skills_some)
    return 6 * title_score(job.title, profile.target_roles) + 2 * strong + some


def rank(jobs: list[tuple[str, Job]], profile: Profile) -> list[str]:
    """Job keys, best first. Ties keep the input order."""
    scored = [(-score(job, profile), i, key) for i, (key, job) in enumerate(jobs)]
    return [key for _, _, key in sorted(scored)]
