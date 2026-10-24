"""SmartRecruiters adapter: reads a company's public postings API (India jobs only).

The list has no descriptions, so each new job costs one extra call. Jobs we already
store come back without a description, and the store keeps the saved one.
"""

import sys
import time

from src import normalize as n
from src.collect.http import get_json
from src.models import Job, Source

API = "https://api.smartrecruiters.com/v1/companies/{board}/postings"
PAGE = 100
DETAIL_DELAY = 0.2  # seconds between detail calls
SECTIONS = ("jobDescription", "qualifications", "additionalInformation")  # skips the company blurb


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    url = API.format(board=source.board)
    jobs, offset = [], 0
    while True:
        data = get_json(url, params={"country": "in", "limit": PAGE, "offset": offset})
        items = data.get("content") or []
        for raw in items:
            detail = None
            if raw["id"] not in known:
                time.sleep(DETAIL_DELAY)
                detail = get_json(f"{url}/{raw['id']}")
            jobs.append(to_job(raw, detail, source))
        offset += len(items)
        if not items or offset >= data.get("totalFound", 0):
            return jobs


def to_job(raw: dict, detail: dict | None, source: Source) -> Job:
    loc = raw.get("location") or {}
    ats_mode = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "onsite"
    location = loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
    description = ""
    if detail:
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        description = "\n\n".join(
            f"{s.get('title', '')}\n{n.html_to_text(s.get('text'))}".strip()
            for key in SECTIONS if (s := sections.get(key)) and s.get("text"))
    mode = n.work_mode(location, ats_mode)
    exp_min, exp_max = n.experience_range(description)
    dept = (raw.get("department") or {}).get("label") or (raw.get("function") or {}).get("label")
    company = (raw.get("company") or {}).get("identifier") or source.board
    return Job(
        source=source.name,
        ats_id=str(raw["id"]),
        title=raw["name"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description, (loc.get("country") or "").upper()),
        experience_min=exp_min,
        experience_max=exp_max,
        department=dept,
        description=description,
        apply_url=(detail or {}).get("postingUrl") or f"https://jobs.smartrecruiters.com/{company}/{raw['id']}",
        posted_at=raw.get("releasedDate"),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "Freshworks"
    jobs = fetch(Source(name=board, type="smartrecruiters", board=board))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.india_eligible}  {len(j.description)}")
    print(f"\n{len(jobs)} India jobs on SmartRecruiters company '{board}'")
