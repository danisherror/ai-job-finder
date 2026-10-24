"""Jibe adapter: reads a company's Jibe (iCIMS) career site search API.

`board` is the career site's base URL, e.g. https://careers.amd.com or https://www.github.careers.
This is the public JSON the site itself loads at <board>/api/jobs. It returns 100 jobs per page
with full descriptions, so no extra calls are needed. Sites that support it are asked for India
jobs only; others (GitHub) ignore the filter and return everything, which the rules then sort out.
"""

import sys
import time
from datetime import datetime

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

PAGE_SIZE = 100
MAX_JOBS = 1000     # safety cap per company
PAGE_DELAY = 1.0


def base_url(board: str) -> str:
    return (board if "://" in board else "https://" + board).rstrip("/")


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    base = base_url(source.board)
    cap = source.max_jobs or MAX_JOBS
    jobs: list[Job] = []
    page, total = 1, cap
    while len(jobs) < min(total, cap):
        if page > 1:
            time.sleep(PAGE_DELAY)
        data = get_json(f"{base}/api/jobs", params={"location": "India", "limit": PAGE_SIZE, "page": page})
        total = data.get("totalCount") or 0
        items = data.get("jobs") or []
        if not items:
            break
        jobs += [to_job(item["data"], source) for item in items[:cap - len(jobs)]]
        page += 1
    return jobs


def _posted(raw: dict) -> datetime | None:
    for key in ("posted_date", "create_date"):
        try:
            return datetime.strptime(raw[key], "%Y-%m-%dT%H:%M:%S%z")
        except (KeyError, TypeError, ValueError):
            continue
    return None


def to_job(raw: dict, source: Source) -> Job:
    location = raw.get("full_location") or raw.get("location_name") or raw.get("country") or ""
    if raw.get("multipleLocations") and raw.get("location_name"):
        location = f"{location} | {raw['location_name']}"
    parts = [raw.get("description"), raw.get("responsibilities"), raw.get("qualifications")]
    description = "\n\n".join(n.html_to_text(p) for p in parts if p)
    hint = "remote" if "remote" in f"{raw.get('title', '')} {raw.get('location_name', '')}".lower() else None
    mode = n.work_mode(location, hint)
    exp_min, exp_max = n.experience_range(description)
    cats = raw.get("categories") or []
    return Job(
        source=source.name,
        ats_id=str(raw.get("slug") or raw["req_id"]),
        title=raw["title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description, raw.get("country_code")),
        experience_min=exp_min,
        experience_max=exp_max,
        department=cats[0].get("name") if cats and isinstance(cats[0], dict) else None,
        description=description,
        apply_url=f"{base_url(source.board)}/careers-home/jobs/{raw.get('slug') or raw['req_id']}",
        posted_at=_posted(raw),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "https://careers.amd.com"
    jobs = fetch(Source(name="test", type="jibe", board=board))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.work_mode:<7}  {j.india_eligible}")
    print(f"\n{len(jobs)} jobs on {board}")
