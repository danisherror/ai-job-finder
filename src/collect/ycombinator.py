"""Y Combinator jobs adapter: startup jobs from ycombinator.com/jobs location pages.

`board` is a comma-separated list of location slugs, as in ycombinator.com/jobs/location/<slug>,
e.g. "india". Each page lists its jobs as JSON inside the HTML (the data-page attribute), with no
descriptions, so each new job costs one call to its own page. Jobs whose listed location rules out
India ("Remote (US)") are skipped without that call.

This is a job board (many YC startups; `company` is the real employer). Applying needs a free
YC account; the stored link is the job's public page.
"""

import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from src import normalize as n
from src.collect.http import SourceError, get_text
from src.models import Job, Source

SITE = "https://www.ycombinator.com"
LIST = SITE + "/jobs/location/{slug}"
DELAY = 1.0
_PAGE_RE = re.compile(r'data-page="([^"]+)"')
_AGE_RE = re.compile(r"(\d+)\s*(minute|hour|day|week|month|year)s?", re.I)
_UNIT_DAYS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}
# YC writes countries as two-letter codes ("US / IN / SG / Remote"); "IN" here means India.
_CODES = {"IN": "India", "US": "United States", "SG": "Singapore", "GB": "United Kingdom", "CA": "Canada"}


def page_props(text: str) -> dict:
    m = _PAGE_RE.search(text)
    if not m:
        raise SourceError("bad_response", "no job data on the page")
    try:
        return json.loads(html.unescape(m.group(1))).get("props") or {}
    except ValueError as e:
        raise SourceError("bad_response", "job data is not JSON") from e


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    for i, slug in enumerate(s.strip() for s in source.board.split(",") if s.strip()):
        if i:
            time.sleep(DELAY)
        for raw in page_props(get_text(LIST.format(slug=slug))).get("jobPostings") or []:
            ats_id, loc = str(raw["id"]), location_of(raw)
            if ats_id in jobs or n.india_eligibility(loc, n.work_mode(loc)) == "no":
                continue
            detail = None
            if ats_id not in known:
                time.sleep(DELAY)
                detail = page_props(get_text(SITE + raw["url"])).get("job")
            jobs[ats_id] = to_job(raw, detail, source)
    return list(jobs.values())


def location_of(raw: dict) -> str:
    parts = [p.strip() for p in (raw.get("location") or "").split("/") if p.strip()]
    return " | ".join(_CODES.get(p, p) for p in parts)


def posted_at(created: str | None, now: datetime | None = None) -> datetime | None:
    """ "2 days" -> about two days ago (YC only gives the age)."""
    m = _AGE_RE.search(created or "")
    if not m:
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()])


def to_job(raw: dict, detail: dict | None, source: Source) -> Job:
    location = location_of(raw)
    description = n.html_to_text((detail or {}).get("description"))
    mode = n.work_mode(location)
    exp_min, exp_max = n.experience_range(" ".join(filter(None, [raw.get("minExperience"), description])))
    return Job(
        source=source.name,
        ats_id=str(raw["id"]),
        title=raw["title"].strip(),
        company=(raw.get("companyName") or "").strip() or "Unknown",
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        salary=raw.get("salaryRange") or None,
        department=raw.get("prettyRole") or raw.get("role"),
        description=description,
        apply_url=SITE + raw["url"],
        posted_at=posted_at(raw.get("createdAt")),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "india"
    jobs = fetch(Source(name="YC Jobs", type="ycombinator", board=board))
    for j in jobs:
        print(f"{j.company[:20]:<20}  {j.title[:50]:<50}  {j.location_raw[:30]:<30}  {j.india_eligible}")
    print(f"\n{len(jobs)} YC jobs for {board}")
