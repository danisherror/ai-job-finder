"""Rippling ATS adapter: reads a company's public Rippling job board API (India jobs only).

`board` is the board name in ats.rippling.com/<board>/jobs. The list has only title and
location, so each new India job costs one extra call for its description and date.
"""

import sys
import time

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs"
DETAIL_DELAY = 0.2


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    url = API.format(board=source.board)
    jobs = []
    for raw in get_json(url) or []:
        if not n.mentions_india(_label(raw)):
            continue
        detail = None
        if raw["uuid"] not in known:
            time.sleep(DETAIL_DELAY)
            detail = get_json(f"{url}/{raw['uuid']}")
        jobs.append(to_job(raw, detail, source))
    return jobs


def _label(raw: dict) -> str:
    return (raw.get("workLocation") or {}).get("label") or ""


def to_job(raw: dict, detail: dict | None, source: Source) -> Job:
    d = detail or {}
    location = " | ".join(dict.fromkeys(d.get("workLocations") or [_label(raw)]))
    desc = d.get("description") or {}
    description = n.html_to_text(desc.get("role") if isinstance(desc, dict) else desc)  # skips the company blurb
    mode = n.work_mode(location)
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=raw["uuid"],
        title=raw["name"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=(raw.get("department") or {}).get("label"),
        description=description,
        apply_url=raw.get("url") or f"https://ats.rippling.com/{source.board}/jobs/{raw['uuid']}",
        posted_at=d.get("createdOn"),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "rippling"
    jobs = fetch(Source(name=board, type="rippling", board=board))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.india_eligible}  {len(j.description)}")
    print(f"\n{len(jobs)} India jobs on Rippling board '{board}'")
