"""Workable adapter: reads a company's public job widget feed (all jobs, with descriptions)."""

import sys

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://apply.workable.com/api/v1/widget/accounts/{board}"


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    data = get_json(API.format(board=source.board), params={"details": "true"})
    return [to_job(raw, source) for raw in data.get("jobs") or []]


def to_job(raw: dict, source: Source) -> Job:
    places = raw.get("locations") or [{"city": raw.get("city"), "region": raw.get("state"),
                                       "country": raw.get("country")}]
    locs = [", ".join(p for p in (l.get("city"), l.get("region"), l.get("country")) if p) for l in places]
    if raw.get("telecommuting"):
        locs.append("Remote")
    location = " | ".join(dict.fromkeys(l for l in locs if l))
    codes = {l.get("countryCode") for l in places}
    description = n.html_to_text(raw.get("description"))
    mode = n.work_mode(location, "remote" if raw.get("telecommuting") else None)
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=raw["shortcode"],
        title=raw["title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description, "IN" if "IN" in codes else None),
        experience_min=exp_min,
        experience_max=exp_max,
        department=raw.get("department") or raw.get("function") or None,
        description=description,
        apply_url=raw.get("url") or raw["shortlink"],
        posted_at=raw.get("published_on") or raw.get("created_at"),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "huggingface"
    jobs = fetch(Source(name=board, type="workable", board=board))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.india_eligible}")
    print(f"\n{len(jobs)} jobs on Workable account '{board}'")
