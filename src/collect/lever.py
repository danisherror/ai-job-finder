"""Lever adapter: reads a company's public postings feed."""

from datetime import datetime, timezone

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://api.lever.co/v0/postings/{board}"


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    data = get_json(API.format(board=source.board), params={"mode": "json"})
    if not isinstance(data, list):
        raise ValueError("Lever feed did not return a list")
    return [to_job(raw, source) for raw in data]


def _salary(raw: dict) -> str | None:
    s = raw.get("salaryRange")
    if not s or s.get("min") is None:
        return None
    return f"{s.get('currency', '')} {s['min']}-{s.get('max', '')} per {s.get('interval', '')}".strip()


def to_job(raw: dict, source: Source) -> Job:
    cat = raw.get("categories") or {}
    locs = [cat.get("location") or ""] + list(cat.get("allLocations") or [])
    location = " | ".join(dict.fromkeys(l for l in locs if l))
    parts = [raw.get("descriptionPlain") or ""]
    for lst in raw.get("lists") or []:
        parts.append(f"{lst.get('text', '')}\n{n.html_to_text(lst.get('content'))}")
    parts.append(raw.get("additionalPlain") or "")
    description = "\n\n".join(p.strip() for p in parts if p and p.strip())
    country = raw.get("country")
    mode = n.work_mode(location, raw.get("workplaceType"))
    exp_min, exp_max = n.experience_range(description)
    created = raw.get("createdAt")
    return Job(
        source=source.name,
        ats_id=raw["id"],
        title=raw["text"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description, country),
        experience_min=exp_min,
        experience_max=exp_max,
        salary=_salary(raw),
        department=cat.get("department") or cat.get("team"),
        description=description,
        apply_url=raw.get("applyUrl") or raw["hostedUrl"],
        posted_at=datetime.fromtimestamp(created / 1000, tz=timezone.utc) if created else None,
    )
