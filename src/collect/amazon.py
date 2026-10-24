"""Amazon adapter: reads amazon.jobs' public search JSON (the same data the search page loads).

`board` is a country code as amazon.jobs uses it, e.g. "IND". Descriptions come with the list,
so there are no per-job calls. Only tech job categories are kept: India alone has 2,000+
Amazon jobs, most of them in warehouses, HR and operations.
"""

import sys
import time
from datetime import datetime, timezone

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://www.amazon.jobs/en/search.json"
SITE = "https://www.amazon.jobs"
PAGE = 100
MAX_JOBS = 3000
PAGE_DELAY = 0.5
TECH_CATEGORIES = {
    "Software Development", "Applied Science", "Data Science", "Machine Learning Science", "Research Science",
    "Business Intelligence", "Solutions Architect", "Systems, Quality, & Security Engineering",
    "Project/Program/Product Management--Technical", "Hardware Development", "Database Administration",
    "Operations, IT, & Support Engineering", "Design",
}


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: list[Job] = []
    for offset in range(0, MAX_JOBS, PAGE):
        if offset:
            time.sleep(PAGE_DELAY)
        data = get_json(API, params={"country": source.board, "result_limit": PAGE, "offset": offset,
                                     "sort": "recent"})
        items = data.get("jobs") or []
        jobs += [to_job(r, source) for r in items if r.get("job_category") in TECH_CATEGORIES]
        if len(items) < PAGE or offset + PAGE >= (data.get("hits") or 0):
            break
    return jobs


def _posted(raw: dict) -> datetime | None:
    try:
        return datetime.strptime(raw.get("posted_date") or "", "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_job(raw: dict, source: Source) -> Job:
    location = raw.get("normalized_location") or raw.get("location") or ""
    location = location.removesuffix(", IND") + ", India" if location.endswith(", IND") else location
    parts = [("", raw.get("description")), ("Basic qualifications", raw.get("basic_qualifications")),
             ("Preferred qualifications", raw.get("preferred_qualifications"))]
    description = "\n\n".join(f"{title}\n{n.html_to_text(text)}".strip() for title, text in parts if text)
    mode = n.work_mode(location)
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=str(raw["id_icims"]),
        title=raw["title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description,
                                           "IN" if raw.get("country_code") == "IND" else None),
        experience_min=exp_min,
        experience_max=exp_max,
        department=raw.get("job_category"),
        description=description,
        apply_url=SITE + raw["job_path"],
        posted_at=_posted(raw),
    )


if __name__ == "__main__":
    country = sys.argv[1] if len(sys.argv) > 1 else "IND"
    jobs = fetch(Source(name="Amazon", type="amazon", board=country))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.india_eligible}  {j.department}")
    print(f"\n{len(jobs)} tech jobs at Amazon in {country}")
