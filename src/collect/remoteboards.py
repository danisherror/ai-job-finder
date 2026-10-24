"""Job boards with a free public feed: Remote OK, Remotive, Working Nomads, Jobspresso and Hasjob.
Each exposes its own fetch(source, known).

These are job boards (many companies; `company` is the real employer). Each read is one call
returning the newest jobs, so they run once per scan. The Apply link is the job's page on the
board, which Remote OK and Remotive ask for in return for using their APIs.
`board` is unused except by Remotive, where it is the category slug (e.g. "software-dev").
"""

import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from src import normalize as n
from src.collect.http import SourceError, get_json, get_text
from src.models import Job, Source

MAX_AGE_DAYS = 30
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


def _iso(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _rfc822(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def _remote_location(place: str | None) -> str:
    place = (place or "").strip()
    return f"Remote - {place}" if place else "Remote"


def _job(source: Source, ats_id: str, title: str, company: str, place: str | None, html: str | None,
         url: str, posted: datetime | None, department: str | None = None, salary: str | None = None) -> Job:
    location = _remote_location(place)
    description = n.html_to_text(html)
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=ats_id,
        title=title.strip(),
        company=(company or "").strip() or "Unknown",
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode="remote",
        india_eligible=n.india_eligibility(location, "remote", description),
        experience_min=exp_min,
        experience_max=exp_max,
        salary=salary,
        department=department,
        description=description,
        apply_url=url,
        posted_at=posted,
    )


def _recent(jobs: list[Job]) -> list[Job]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    unique = {j.ats_id: j for j in jobs}
    return [j for j in unique.values() if j.posted_at is None or j.posted_at >= cutoff]


def _rss_items(text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SourceError("bad_response", "not RSS") from e
    return [{child.tag: (child.text or "").strip() for child in item} for item in root.iter("item")]


# ---------------------------------------------------------------- Remote OK

REMOTEOK_API = "https://remoteok.com/api"


def remoteok_job(raw: dict, source: Source) -> Job:
    lo, hi = raw.get("salary_min") or 0, raw.get("salary_max") or 0
    salary = f"USD {lo}-{hi} yearly" if lo or hi else None
    tags = raw.get("tags") or []
    url = raw.get("url") or f"https://remoteok.com/remote-jobs/{raw['id']}"
    return _job(source, url, raw["position"], raw.get("company"), raw.get("location"), raw.get("description"),
                url, _iso(raw.get("date")), department=tags[0] if tags else None, salary=salary)


def fetch_remoteok(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    data = get_json(REMOTEOK_API)
    # The first item is the API's legal notice, not a job.
    return _recent([remoteok_job(r, source) for r in data if isinstance(r, dict) and r.get("position")])


# ---------------------------------------------------------------- Remotive

REMOTIVE_API = "https://remotive.com/api/remote-jobs"   # they ask for at most a few calls a day


def remotive_job(raw: dict, source: Source) -> Job:
    return _job(source, raw["url"], raw["title"], raw.get("company_name"), raw.get("candidate_required_location"),
                raw.get("description"), raw["url"], _iso(raw.get("publication_date")),
                department=raw.get("category"), salary=raw.get("salary") or None)


def fetch_remotive(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    data = get_json(REMOTIVE_API, params={"category": source.board or "software-dev"})
    return _recent([remotive_job(r, source) for r in data.get("jobs") or []])


# ---------------------------------------------------------------- Working Nomads

WORKINGNOMADS_API = "https://www.workingnomads.com/api/exposed_jobs/"


def workingnomads_job(raw: dict, source: Source) -> Job:
    return _job(source, raw["url"], raw["title"], raw.get("company_name"), raw.get("location"),
                raw.get("description"), raw["url"], _iso(raw.get("pub_date")), department=raw.get("category_name"))


def fetch_workingnomads(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    return _recent([workingnomads_job(r, source) for r in get_json(WORKINGNOMADS_API)])


# ---------------------------------------------------------------- Jobspresso (RSS)

JOBSPRESSO_FEED = "https://jobspresso.co/feed/?post_type=job_listing"


def jobspresso_job(item: dict[str, str], source: Source) -> Job:
    # The creator field holds "Company<br>⚲&nbsp;Place".
    company, _, place = item.get(f"{DC}creator", "").partition("<br>")
    place = n.html_to_text(place).replace("⚲", "").strip()
    html = item.get(f"{CONTENT}encoded") or item.get("description")
    return _job(source, item.get("guid") or item["link"], item["title"], n.html_to_text(company), place, html,
                item["link"], _rfc822(item.get("pubDate")))


def fetch_jobspresso(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    return _recent([jobspresso_job(i, source) for i in _rss_items(get_text(JOBSPRESSO_FEED))])


# ---------------------------------------------------------------- Hasjob (Atom)

HASJOB_FEED = "https://hasjob.co/feed"
ATOM = "{http://www.w3.org/2005/Atom}"
_HASJOB_COMPANY_RE = re.compile(r"<strong>\s*<a [^>]*>(.*?)</a>", re.S)


def hasjob_job(entry: ET.Element, source: Source) -> Job:
    """Hasjob is an Indian startup board, so an office location without a country is taken to be in India."""
    html = entry.findtext(f"{ATOM}content") or ""
    url = entry.findtext(f"{ATOM}id") or entry.find(f"{ATOM}link").get("href")
    company = _HASJOB_COMPANY_RE.search(html)
    place = (entry.findtext(f"{ATOM}location") or "").strip()
    mode = n.work_mode(place)
    location = place if mode == "remote" or n.mentions_india(place) else f"{place}, India".lstrip(", ")
    description = n.html_to_text(html)
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=url,
        title=(entry.findtext(f"{ATOM}title") or "").strip(),
        company=n.html_to_text(company.group(1)) if company else url.split("/")[3],
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        description=description,
        apply_url=url,
        posted_at=_iso(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")),
    )


def fetch_hasjob(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    try:
        root = ET.fromstring(get_text(HASJOB_FEED))
    except ET.ParseError as e:
        raise SourceError("bad_response", "not Atom") from e
    return _recent([hasjob_job(e, source) for e in root.iter(f"{ATOM}entry")])


FETCHERS = {
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "workingnomads": fetch_workingnomads,
    "jobspresso": fetch_jobspresso,
    "hasjob": fetch_hasjob,
}


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "remoteok"
    jobs = FETCHERS[kind](Source(name=kind, type=kind, board=sys.argv[2] if len(sys.argv) > 2 else ""))
    for j in jobs:
        print(f"{j.company[:20]:<20}  {j.title[:50]:<50}  {j.location_raw[:30]:<30}  {j.india_eligible}")
    print(f"\n{len(jobs)} recent jobs on {kind}")
