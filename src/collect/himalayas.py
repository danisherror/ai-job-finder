"""Himalayas adapter: remote jobs open to one country, from the free Himalayas jobs API.

`board` is the country name, e.g. "India". Jobs open "worldwide" are left out: most are
gig work, and India-specific listings match far better.

Unlike a company feed this is a job board, so it holds many companies (`company` is the
real employer), the same job may also come from a company feed (the store skips those),
and it only reads the newest pages, so a job missing from a scan does not mean it closed.
Himalayas asks that the Apply link is their applicationLink, which is what we store.
"""

import sys
import time
from datetime import datetime, timezone

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://himalayas.app/jobs/api/search"
MAX_PAGES = 25         # 20 jobs per page
MAX_AGE_DAYS = 14      # stop paging once jobs get older than this
PAGE_DELAY = 1.0


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: list[Job] = []
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAGE_DELAY)
        data = get_json(API, params={"country": source.board, "exclude_worldwide": "true",
                                     "sort": "recent", "page": page})
        items = data.get("jobs") or []
        fresh = [r for r in items if (r.get("pubDate") or 0) >= cutoff]
        jobs += [to_job(r, source) for r in fresh]
        # Stop at the end, at old jobs, or once a whole page is already known (we've caught up).
        if len(fresh) < len(items) or not items or all(r["guid"] in known for r in items):
            break
    return jobs


def _salary(raw: dict) -> str | None:
    lo, hi = raw.get("minSalary"), raw.get("maxSalary")
    if not lo and not hi:
        return None
    amount = f"{lo or ''}-{hi or ''}".strip("-")
    return f"{raw.get('currency') or ''} {amount} {raw.get('salaryPeriod') or ''}".strip()


def to_job(raw: dict, source: Source) -> Job:
    places = raw.get("locationRestrictions") or []
    location = " | ".join(places + ["Remote"])
    description = n.html_to_text(raw.get("description"))
    exp_min, exp_max = n.experience_range(description)
    pub = raw.get("pubDate")
    cats = raw.get("parentCategories") or raw.get("categories") or []
    return Job(
        source=source.name,
        ats_id=raw["guid"],
        title=raw["title"].strip(),
        company=(raw.get("companyName") or "").strip() or "Unknown",
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode="remote",
        india_eligible=n.india_eligibility(location, "remote", description),
        experience_min=exp_min,
        experience_max=exp_max,
        salary=_salary(raw),
        department=cats[0].replace("-", " ") if cats else None,
        description=description,
        apply_url=raw.get("applicationLink") or raw["guid"],
        posted_at=datetime.fromtimestamp(pub, tz=timezone.utc) if pub else None,
    )


if __name__ == "__main__":
    country = sys.argv[1] if len(sys.argv) > 1 else "India"
    jobs = fetch(Source(name="Himalayas", type="himalayas", board=country))
    for j in jobs:
        print(f"{j.company[:20]:<20}  {j.title[:50]:<50}  {j.location_raw[:25]:<25}  {j.india_eligible}")
    print(f"\n{len(jobs)} recent jobs on Himalayas for {country}")
