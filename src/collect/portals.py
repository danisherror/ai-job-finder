"""Job portals with an official API that needs a key: Jooble and Careerjet.
Each exposes its own fetch(source, known). Keys come from .env (see .env.example).

`board` holds the search keywords, several separated by "|"; each one is a separate search.
These are job boards (many companies; `company` is the real employer) and they only return a
short snippet of each description, not the full text. The Apply link goes through the portal's
own redirect page, which is what both portals ask for.
"""

import hashlib
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from src import normalize as n
from src.collect.http import SourceError, get_json, post_json
from src.models import Job, Source

MAX_AGE_DAYS = 14
PAGE_DELAY = 1.0


def _key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SourceError("no_api_key", f"{name} is not set in .env")
    return value


def _keywords(source: Source) -> list[str]:
    return [k.strip() for k in source.board.split("|") if k.strip()] or ["software developer"]


def _in_india(place: str) -> str:
    """Both searches are limited to India, so a place without a country is in India."""
    place = (place or "").strip()
    if not place:
        return "India"
    return place if n.mentions_india(place) or n.work_mode(place) == "remote" else f"{place}, India"


def _job(source: Source, ats_id: str, title: str, company: str, place: str, snippet: str, url: str,
         posted: datetime | None, salary: str | None, job_type: str | None = None) -> Job:
    location = _in_india(place)
    description = n.html_to_text(snippet).strip(" .")
    mode = n.work_mode(f"{location} {title}")
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=ats_id,
        title=n.html_to_text(title).strip(),
        company=n.html_to_text(company).strip() or "Unknown",
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        salary=(salary or "").strip() or None,
        department=(job_type or "").strip() or None,
        description=description,
        apply_url=url,
        posted_at=posted,
    )


# ---------------------------------------------------------------- Jooble

# A key works only on the Jooble site it was issued for. A key from in.jooble.org would need
# JOOBLE_API_URL=https://in.jooble.org/api/ in .env. The default key allows 500 requests in all,
# so each keyword costs one request per scan unless a page is full.
JOOBLE_API = "https://jooble.org/api/"
JOOBLE_LOCATION = "India"     # the global site finds nothing for city names
JOOBLE_PAGE_SIZE = 100
JOOBLE_MAX_PAGES = 2


def _jooble_date(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


def jooble_job(raw: dict, source: Source) -> Job:
    return _job(source, str(raw["id"]), raw["title"], raw.get("company") or "", raw.get("location") or "",
                raw.get("snippet") or "", raw["link"], _jooble_date(raw.get("updated")), raw.get("salary"),
                raw.get("type"))


def fetch_jooble(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    url = (os.environ.get("JOOBLE_API_URL") or JOOBLE_API).rstrip("/") + "/" + _key("JOOBLE_API_KEY")
    since = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    jobs: dict[str, Job] = {}
    for keyword in _keywords(source):
        for page in range(1, JOOBLE_MAX_PAGES + 1):
            time.sleep(PAGE_DELAY)
            data = post_json(url, {"keywords": keyword, "location": JOOBLE_LOCATION, "page": str(page),
                                   "ResultOnPage": str(JOOBLE_PAGE_SIZE), "datecreatedfrom": since})
            items = data.get("jobs") or []
            for raw in items:
                jobs.setdefault(str(raw["id"]), jooble_job(raw, source))
            if len(items) < JOOBLE_PAGE_SIZE or all(str(r["id"]) in known for r in items):
                break
    return list(jobs.values())


# ---------------------------------------------------------------- Careerjet

# The public API only answers over plain HTTP and refuses calls without a Referer header.
# Its links are tracking URLs that change on every call, so a job's ID is built from its
# title, company and location instead.
CAREERJET_API = "http://public.api.careerjet.net/search"
CAREERJET_REFERER = "https://localhost/ai-job-finder"
CAREERJET_PAGE_SIZE = 50      # the API's maximum
CAREERJET_MAX_PAGES = 10      # per keyword; a busy keyword gets hundreds of India jobs a day


def careerjet_id(raw: dict) -> str:
    parts = (raw.get("title") or "", raw.get("company") or "", raw.get("locations") or "")
    text = "|".join(" ".join(n.html_to_text(p).lower().split()) for p in parts)
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _rfc822(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def careerjet_job(raw: dict, source: Source) -> Job:
    return _job(source, careerjet_id(raw), raw["title"], raw.get("company") or "", raw.get("locations") or "",
                raw.get("description") or "", raw["url"], _rfc822(raw.get("date")), raw.get("salary"))


def fetch_careerjet(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    params = {"affid": _key("CAREERJET_API_KEY"), "locale_code": "en_IN", "location": "India",
              "sort": "date", "pagesize": CAREERJET_PAGE_SIZE, "user_ip": "127.0.0.1",
              "user_agent": "ai-job-finder"}
    headers = {"Referer": os.environ.get("CAREERJET_REFERER") or CAREERJET_REFERER}
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    jobs: dict[str, Job] = {}
    for keyword in _keywords(source):
        for page in range(1, CAREERJET_MAX_PAGES + 1):
            time.sleep(PAGE_DELAY)
            data = get_json(CAREERJET_API, params={**params, "keywords": keyword, "page": page}, headers=headers)
            if data.get("type") == "ERROR":
                raise SourceError("blocked", str(data.get("error")))
            items = data.get("jobs") or []
            batch = [careerjet_job(r, source) for r in items]
            fresh = [j for j in batch if j.posted_at is None or j.posted_at >= cutoff]
            for job in fresh:
                jobs.setdefault(job.ats_id, job)
            # Stop at the end, at old jobs, or once a whole page is already known (we've caught up).
            if (len(items) < CAREERJET_PAGE_SIZE or len(fresh) < len(batch) or page >= (data.get("pages") or 0)
                    or all(j.ats_id in known for j in batch)):
                break
    return list(jobs.values())


FETCHERS = {
    "jooble": fetch_jooble,
    "careerjet": fetch_careerjet,
}


if __name__ == "__main__":
    from src.config import load_env

    load_env()
    kind = sys.argv[1] if len(sys.argv) > 1 else "careerjet"
    found = FETCHERS[kind](Source(name=kind, type=kind, board=sys.argv[2] if len(sys.argv) > 2 else ""))
    for j in found:
        print(f"{j.company[:20]:<20}  {j.title[:50]:<50}  {j.location_raw[:30]:<30}  {j.india_eligible}")
    print(f"\n{len(found)} recent jobs on {kind}")
