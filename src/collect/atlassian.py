"""Atlassian adapter: reads the job list Atlassian's own careers page loads.

Atlassian's jobs live on iCIMS classic, but atlassian.com/company/careers/all-jobs loads every open
job (with its full description) from one public JSON list, so one call per scan is enough.
`board` is unused. Only jobs with an India location are kept; Atlassian's "Remote - Remote" jobs
are usually limited to countries where it has an office, so those are left to the India location.
"""

from datetime import datetime, timezone

from src import normalize as n
from src.collect.http import SourceError, get_json
from src.models import Job, Source

LISTINGS = "https://www.atlassian.com/endpoint/careers/listings"


def _posted(raw: dict) -> datetime | None:
    value = (raw.get("portalJobPost") or {}).get("updatedDate")
    try:
        return datetime.strptime(value, "%Y-%m-%d %I:%M %p").replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


def _place(location: str) -> str:
    """"Bengaluru - India -   Bengaluru,  560071 India" -> "Bengaluru, India"."""
    parts = [p.strip() for p in location.split(" - ")]
    return ", ".join(dict.fromkeys(p for p in parts[:2] if p)) if len(parts) >= 2 else location.strip()


def to_job(raw: dict, source: Source) -> Job:
    places = [_place(loc) for loc in raw.get("locations") or []]
    india = [p for p in places if n.mentions_india(p)]
    location = " | ".join(dict.fromkeys(india or places))
    parts = [raw.get("overview"), raw.get("responsibilities"), raw.get("qualifications")]
    description = "\n\n".join(n.html_to_text(p) for p in parts if p)
    mode = n.work_mode(location)
    exp_min, exp_max = n.experience_range(description)
    url = (raw.get("portalJobPost") or {}).get("portalUrl") or raw.get("applyUrl")
    return Job(
        source=source.name,
        ats_id=str(raw["id"]),
        title=raw["title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=raw.get("category"),
        description=description,
        apply_url=url,
        posted_at=_posted(raw),
    )


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    data = get_json(LISTINGS)
    if not isinstance(data, list):
        raise SourceError("bad_response", "expected a list of jobs")
    india = [r for r in data if any(n.mentions_india(loc) for loc in r.get("locations") or [])]
    return [to_job(r, source) for r in india]


if __name__ == "__main__":
    jobs = fetch(Source(name="Atlassian", type="atlassian", board=""))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:40]:<40}  {j.work_mode:<7}  {j.india_eligible}")
    print(f"\n{len(jobs)} India jobs at Atlassian")
