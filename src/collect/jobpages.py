"""Career sites read through their sitemap and job pages: TalentBrew, iCIMS classic and Societe Generale.

These sites have no job API that robots.txt allows, but they publish a sitemap listing every job page,
and each job page carries the job as schema.org JobPosting data (the structured data search engines
read): title, location, date and full description. So a scan reads the sitemap once, picks the job
pages, and opens only pages it hasn't stored before (at most MAX_NEW per scan; the rest wait for the
next scan). Known jobs are returned without a title or description; the store keeps what it saved.

`board` is the career site's base URL, e.g. https://careers.arm.com. Per type:
- talentbrew (Radancy TalentBrew sites: Arm, Synopsys, UnitedHealth Group / Optum): job URLs read
  /job/<city>/<title>/<org>/<id>, so only jobs whose city is in India are opened.
- icims (iCIMS classic, e.g. https://ideasglobal-sas.icims.com): job URLs carry no place, so every
  job is opened once. Only for small sites.
- socgen (careers.societegenerale.com): the home page lists every job's country, so only India
  jobs are opened.
"""

import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from src import normalize as n
from src.collect.http import SourceError, get_text
from src.models import Job, Source

MAX_NEW = 200       # new job pages opened per scan
DELAY = 1.0
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_EMPTY = {"", "unavailable", "n/a", "na"}
COUNTRY_CODES = {"IN": "India", "IND": "India"}


def base_url(board: str) -> str:
    return (board if "://" in board else "https://" + board).rstrip("/")


def sitemap_urls(board: str) -> list[str]:
    return [html.unescape(u) for u in _LOC_RE.findall(get_text(f"{base_url(board)}/sitemap.xml"))]


# ---------------------------------------------------------------- job page

def job_posting(page: str) -> dict | None:
    """The schema.org JobPosting on a job page, if any."""
    for block in _LD_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph") or [data] if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _places(posting: dict) -> list[str]:
    raw = posting.get("jobLocation") or []
    places = []
    for loc in raw if isinstance(raw, list) else [raw]:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            places.append(addr)
            continue
        country = str(addr.get("addressCountry") or "")
        country = COUNTRY_CODES.get(country.upper(), country) if not isinstance(addr.get("addressCountry"), dict) \
            else addr["addressCountry"].get("name", "")
        parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
        places.append(", ".join(dict.fromkeys(str(p).strip() for p in parts if p and str(p).strip().lower() not in _EMPTY)))
    return [p for p in places if p]


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("/", "-").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_job(ats_id: str, url: str, posting: dict | None, source: Source) -> Job:
    """A job from its JobPosting data; with no data (a known job not opened again) only the ID and link."""
    p = posting or {}
    location = " | ".join(dict.fromkeys(_places(p)))
    description = n.html_to_text(p.get("description"))
    remote = str(p.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    mode = n.work_mode(location, is_remote=remote) if posting else "unknown"
    exp_min, exp_max = n.experience_range(description)
    kind = p.get("employmentType")
    return Job(
        source=source.name,
        ats_id=ats_id,
        title=html.unescape(str(p.get("title") or "")).strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description) if posting else "unclear",
        experience_min=exp_min,
        experience_max=exp_max,
        department=(", ".join(kind) if isinstance(kind, list) else kind) or None,
        description=description,
        apply_url=url,
        posted_at=_date(p.get("datePosted")),
    )


def read_pages(source: Source, pages: list[tuple[str, str]], known: frozenset[str],
               page_params: dict | None = None) -> list[Job]:
    """pages: (job ID, job page URL). Opens new pages (up to MAX_NEW); known ones come back bare."""
    jobs, opened = [], 0
    for ats_id, url in dict(pages).items():
        if ats_id in known:
            jobs.append(to_job(ats_id, url, None, source))
            continue
        if opened >= MAX_NEW:
            continue    # left for the next scan; a job saved without its page would stay blank
        time.sleep(DELAY)
        opened += 1
        try:
            posting = job_posting(get_text(url, params=page_params))
        except SourceError as e:
            if e.reason == "not_found":
                continue    # taken down since the sitemap was built
            raise
        if posting:
            jobs.append(to_job(ats_id, url, posting, source))
    return jobs


# ---------------------------------------------------------------- TalentBrew

_TALENTBREW_RE = re.compile(r"/job/([^/]+)/[^/]+/\d+/(\d+)/?$")


def talentbrew_pages(urls: list[str]) -> list[tuple[str, str]]:
    """India job pages: the first part of the path is the job's (first) city."""
    found = []
    for url in urls:
        m = _TALENTBREW_RE.search(urlsplit(url).path)
        if m:
            place = m.group(1).replace("-", " ")
            if place == "india" or n.indian_cities(place):
                found.append((m.group(2), url))
    return found


def fetch_talentbrew(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    return read_pages(source, talentbrew_pages(sitemap_urls(source.board)), known)


# ---------------------------------------------------------------- iCIMS classic

_ICIMS_RE = re.compile(r"/jobs/(\d+)/[^/]+/job/?$")


def icims_pages(urls: list[str]) -> list[tuple[str, str]]:
    return [(m.group(1), url) for url in urls if (m := _ICIMS_RE.search(urlsplit(url).path))]


def fetch_icims(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    # The plain job page is a frame around the real one; in_iframe=1 asks for the real one.
    return read_pages(source, icims_pages(sitemap_urls(source.board)), known, {"in_iframe": 1})


# ---------------------------------------------------------------- Societe Generale

_SETTINGS_RE = re.compile(r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>', re.S)
_SOCGEN_RE = re.compile(r"/en/job-offers/[^/]*-([0-9A-Z]{8})-en/?$")


def socgen_india_refs(home: str) -> set[str]:
    """Job references in India, from the job-to-country list the home page carries for its search box."""
    m = _SETTINGS_RE.search(home)
    try:
        offers = json.loads(m.group(1))["quantum"]["all_offers"] if m else None
    except (ValueError, KeyError) as e:
        raise SourceError("bad_response", "no job list on the home page") from e
    if not isinstance(offers, dict):
        raise SourceError("bad_response", "no job list on the home page")
    return {ref for ref, place in offers.items() if (place or {}).get("country") == "IND"}


def socgen_pages(urls: list[str], india: set[str]) -> list[tuple[str, str]]:
    return [(m.group(1), url) for url in urls if (m := _SOCGEN_RE.search(urlsplit(url).path)) and m.group(1) in india]


def fetch_socgen(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    india = socgen_india_refs(get_text(f"{base_url(source.board)}/en"))
    return read_pages(source, socgen_pages(sitemap_urls(source.board), india), known)


FETCHERS = {
    "talentbrew": fetch_talentbrew,
    "icims": fetch_icims,
    "socgen": fetch_socgen,
}


if __name__ == "__main__":
    kind, board = sys.argv[1], sys.argv[2]
    MAX_NEW = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    found = FETCHERS[kind](Source(name="test", type=kind, board=board))
    for j in found:
        print(f"{j.title[:50]:<50}  {j.location_raw[:35]:<35}  {j.work_mode:<7} {j.india_eligible:<7} "
              f"{j.posted_at and j.posted_at.date()}  {len(j.description)}")
    print(f"\n{len(found)} jobs on {board}")
