"""Phenom adapter: reads a company's Phenom career site search (India jobs only).

`board` is the career site's base URL with its locale path, e.g.
https://careers.nttdata.com/global/en or https://careers.cencora.com/us/en
The site's own search page names the settings its search call needs (refNum, locale, country), so
each scan reads that page once, then asks the same /widgets search the page uses for country=India.
The list has only a short teaser, so each new job costs one extra call for its description.
"""

import re
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit

from src import normalize as n
from src.collect.http import SourceError, get_text, post_json
from src.models import Job, Source

PAGE_SIZE = 50
MAX_JOBS = 1000     # safety cap per company
DELAY = 1.0
_SETTING_RE = {key: re.compile(rf'"{key}"\s*:\s*"([^"]+)"') for key in ("refNum", "locale", "country")}


def base_url(board: str) -> str:
    return (board if "://" in board else "https://" + board).rstrip("/")


def settings(board: str) -> dict[str, str]:
    """refNum, locale and country, read from the site's search page."""
    page = get_text(f"{base_url(board)}/search-results")
    found = {key: (rx.search(page).group(1) if rx.search(page) else "") for key, rx in _SETTING_RE.items()}
    if not found["refNum"]:
        raise SourceError("bad_response", "no Phenom refNum on the search page")
    return found


def _widgets(board: str) -> str:
    u = urlsplit(base_url(board))
    return f"{u.scheme}://{u.netloc}/widgets"


def _body(cfg: dict[str, str], **extra) -> dict:
    return {"lang": cfg["locale"], "deviceType": "desktop", "country": cfg["country"], "refNum": cfg["refNum"],
            "siteType": "external", **extra}


def _posted(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z") if value else None
    except ValueError:
        return None


def to_job(raw: dict, detail: dict | None, source: Source) -> Job:
    d = detail or {}
    location = " | ".join(dict.fromkeys(raw.get("multi_location") or [raw.get("location") or ""]))
    description = n.html_to_text(d.get("description"))
    mode = n.work_mode(location, raw.get("workplaceType") or raw.get("remote"))
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=str(raw["jobId"]),
        title=raw["title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=raw.get("category") or None,
        description=description,
        apply_url=f"{base_url(source.board)}/job/{raw['jobId']}",
        posted_at=_posted(raw.get("postedDate") or raw.get("dateCreated")),
    )


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    cfg = settings(source.board)
    url = _widgets(source.board)
    cap = source.max_jobs or MAX_JOBS
    jobs: list[Job] = []
    start, total = 0, cap
    while start < min(total, cap):
        time.sleep(DELAY)
        data = post_json(url, _body(cfg, pageName="search-results", ddoKey="refineSearch", sortBy="Most recent",
                                    subsearch="", keywords="", jobs=True, counts=True, all_fields=["country"],
                                    size=PAGE_SIZE, clearAll=False, jdsource="facets", isSliderEnable=False,
                                    pageId="page20", locationData={}, **{"from": start},
                                    selected_fields={"country": ["India"]}))
        search = data.get("refineSearch") or {}
        total = int(search.get("totalHits") or 0)
        items = (search.get("data") or {}).get("jobs") or []
        if not items:
            break
        for raw in items[:cap - start]:
            detail = None
            if str(raw["jobId"]) not in known:
                time.sleep(DELAY)
                found = post_json(url, _body(cfg, pageName="job", ddoKey="jobDetail", jobId=raw["jobId"]))
                detail = ((found.get("jobDetail") or {}).get("data") or {}).get("job")
            jobs.append(to_job(raw, detail, source))
        start += len(items)
    return jobs


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "https://careers.cencora.com/us/en"
    found = fetch(Source(name="test", type="phenom", board=board, max_jobs=15))
    for j in found:
        print(f"{j.title[:50]:<50}  {j.location_raw[:30]:<30}  {j.india_eligible:<7}  {len(j.description)}")
    print(f"\n{len(found)} India jobs on {board}")
