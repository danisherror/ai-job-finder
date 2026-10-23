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
