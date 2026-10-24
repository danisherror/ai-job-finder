"""Greenhouse adapter: reads a company's public job board feed."""

import sys

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    data = get_json(API.format(board=source.board), params={"content": "true"})
    return [to_job(raw, source) for raw in data["jobs"]]


def to_job(raw: dict, source: Source) -> Job:
    locs = [raw.get("location", {}).get("name") or ""]
    locs += [o["name"] for o in raw.get("offices", []) if o.get("name")]
    location = " | ".join(dict.fromkeys(l for l in locs if l))
    description = n.html_to_text(raw.get("content"))
    mode = n.work_mode(location)
    exp_min, exp_max = n.experience_range(description)
    depts = raw.get("departments") or []
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
        department=depts[0]["name"] if depts else None,
        description=description,
        apply_url=raw["absolute_url"],
        posted_at=raw.get("first_published"),
        updated_at=raw.get("updated_at"),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "groww"
    jobs = fetch(Source(name=board, type="greenhouse", board=board))
    for j in jobs:
        print(f"{j.ats_id:>12}  {j.title[:55]:<55}  {j.location_raw[:40]:<40}  {j.india_eligible}")
    print(f"\n{len(jobs)} jobs on Greenhouse board '{board}'")
