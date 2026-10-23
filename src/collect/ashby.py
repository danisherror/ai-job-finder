"""Ashby adapter: reads a company's public job board feed."""

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://api.ashbyhq.com/posting-api/job-board/{board}"


def fetch(source: Source) -> list[Job]:
    data = get_json(API.format(board=source.board), params={"includeCompensation": "true"})
    return [to_job(raw, source) for raw in data["jobs"] if raw.get("isListed", True)]


def to_job(raw: dict, source: Source) -> Job:
    locs = [raw.get("location") or ""] + [s.get("location") or "" for s in raw.get("secondaryLocations") or []]
    country = ((raw.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
    if country:
        locs.append(country)
    location = " | ".join(dict.fromkeys(l for l in locs if l))
    description = raw.get("descriptionPlain") or n.html_to_text(raw.get("descriptionHtml"))
    mode = n.work_mode(location, raw.get("workplaceType"), raw.get("isRemote"))
    exp_min, exp_max = n.experience_range(description)
    comp = raw.get("compensation") or {}
    return Job(
        source=source.name,
        ats_id=raw["id"],
        title=raw["title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        salary=comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary"),
        department=raw.get("department") or raw.get("team"),
        description=description,
        apply_url=raw.get("applyUrl") or raw["jobUrl"],
        posted_at=raw.get("publishedAt"),
    )
