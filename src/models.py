"""Shared data shapes."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

SourceType = Literal["greenhouse", "lever", "ashby"]
WorkMode = Literal["remote", "hybrid", "onsite", "unknown"]
IndiaEligible = Literal["yes", "no", "unclear"]


class Source(BaseModel):
    name: str
    type: SourceType
    board: str
    active: bool = True


class Job(BaseModel):
    """One job opening, normalized from any ATS."""

    source: str                     # Source.name
    ats_id: str                     # the job's ID inside its ATS
    title: str
    company: str
    location_raw: str               # every location string the ATS gave, joined with " | "
    cities: list[str] = Field(default_factory=list)   # normalized Indian cities
    work_mode: WorkMode = "unknown"
    india_eligible: IndiaEligible = "unclear"
    experience_min: int | None = None
    experience_max: int | None = None
    salary: str | None = None
    department: str | None = None
    description: str = ""           # plain text
    apply_url: HttpUrl
    posted_at: datetime | None = None
    updated_at: datetime | None = None


Seniority = Literal["intern", "junior", "mid", "senior", "lead"]
Fit = Literal["strong", "good", "weak"]


class Profile(BaseModel):
    """One person's preferences, from config/profiles/<id>.yaml."""

    id: str                                   # the file name without .yaml
    name: str
    telegram_chat_id: int | None = None
    target_roles: list[str]
    seniority: Seniority
    stretch_up: bool = False                  # also accept jobs one level above `seniority`
    experience_years: float
    skills_strong: list[str] = Field(default_factory=list)
    skills_some: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)       # Indian cities, normalized names
    remote_ok: bool = True
    work_modes: list[Literal["remote", "hybrid", "onsite"]] = Field(
        default_factory=lambda: ["remote", "hybrid", "onsite"])
    min_salary_lpa: float | None = None
    require_keywords: list[str] = Field(default_factory=list)       # at least one in title or description
    exclude_keywords: list[str] = Field(default_factory=list)       # none in title or description
    exclude_title_keywords: list[str] = Field(default_factory=list) # none in the title
    max_age_days: int = 30
    digest_size: int | None = Field(default=None, gt=0)
    resume_file: str | None = None

    @property
    def size(self) -> int:
        return self.digest_size or 8


class MatchResult(BaseModel):
    """The AI's verdict on one job for one person."""

    fit: Fit
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    reason: str = Field(max_length=400)
