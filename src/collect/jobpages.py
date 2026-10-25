"""Sites read through their sitemap and job pages: TalentBrew, iCIMS classic, Societe Generale, Cutshort
and any career site whose sitemap lists its job pages.

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
- cutshort (Indian job board; `board` is empty): its jobs sitemap lists ~45,000 job pages, newest
  first. The employer comes from each page. Only titles that look like tech jobs are opened.
- freshersworld (Indian fresher jobs): category list pages name their 20 newest job pages (schema.org
  ItemList); new ones are opened.
- sitemap (e.g. Uber): `board` is the sitemap URL that lists the job pages; a job page is any URL
  whose last part is a number (its ID). Job URLs carry no place, so every job is opened once.
"""

import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from src import normalize as n
from src.collect.http import BROWSER_HEADERS, SourceError, get_text
from src.models import Job, Source

MAX_NEW = 200       # new job pages opened per scan
DELAY = 1.0
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_EMPTY = {"", "unavailable", "n/a", "na"}
COUNTRY_CODES = {"IN": "India", "IND": "India"}


def base_url(board: str) -> str:
    return (board if "://" in board else "https://" + board).rstrip("/")


def sitemap_urls(board: str, headers: dict | None = None) -> list[str]:
    """Every <loc> in a sitemap; board is the site's base URL or the sitemap's own URL."""
    url = board if board.endswith(".xml") else f"{base_url(board)}/sitemap.xml"
    return [html.unescape(u) for u in _LOC_RE.findall(get_text(url, headers=headers))]


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
    org = p.get("hiringOrganization")
    org = (org.get("name") if isinstance(org, dict) else org) or ""
    return Job(
        source=source.name,
        ats_id=ats_id,
        title=html.unescape(str(p.get("title") or "")).strip(),
        company=html.unescape(str(org)).strip() or "Unknown" if source.aggregator else source.name,
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
               page_params: dict | None = None, headers: dict | None = None) -> list[Job]:
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
            posting = job_posting(get_text(url, params=page_params, headers=headers))
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


# ---------------------------------------------------------------- Cutshort

CUTSHORT_SITEMAP = "https://cutshort-data.s3.amazonaws.com/cloudfront/public/jobs-sitemap.xml"
_CUTSHORT_RE = re.compile(r"^/job/([^/]+)-([A-Za-z0-9]{8})/?$")
# Cutshort lists every kind of job; its titles are in the URL, so only tech-looking ones are opened.
_TECH_RE = re.compile(
    r"engineer|develop|programm|software|sde|devops|sre|cloud|data|machine-learning|\bml\b|ai\b|\bai-|"
    r"analyst|architect|backend|back-end|frontend|front-end|full-?stack|java|python|golang|react|node|android|"
    r"ios|flutter|qa|test|automation|security|cyber|network|sap|salesforce|dotnet|net-|php|ruby|rust|scala|"
    r"kotlin|swift|database|dba|sql|etl|bi-|tech|it-|product-manager|scrum|intern", re.I)


def cutshort_pages(urls: list[str]) -> list[tuple[str, str]]:
    found = []
    for url in urls:
        m = _CUTSHORT_RE.match(urlsplit(url).path)
        if m and _TECH_RE.search(m.group(1)):
            found.append((m.group(2), url))
    return found


def fetch_cutshort(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    urls = sitemap_urls(source.board or CUTSHORT_SITEMAP, headers=BROWSER_HEADERS)
    jobs = read_pages(source, cutshort_pages(urls), known, headers=BROWSER_HEADERS)
    # An Indian board: a remote job with no place given is remote within India.
    return [j.model_copy(update={"location_raw": "Remote, India", "india_eligible": "yes", "work_mode": "remote"})
            if j.title and not j.location_raw else j for j in jobs]


# ---------------------------------------------------------------- Freshersworld

FRESHERSWORLD = "https://www.freshersworld.com/{path}"
FRESHERSWORLD_PAGES = 5           # list pages per category, 20 jobs each, newest first
_FW_ID_RE = re.compile(r"-(\d{5,})/?$")


def item_list(page: str) -> list[str]:
    """Job page URLs from a list page's schema.org ItemList."""
    for block in _LD_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "ItemList":
            return [i["url"] for i in data.get("itemListElement") or [] if isinstance(i, dict) and i.get("url")]
    return []


def fetch_freshersworld(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    """`board`: category pages separated by "|", e.g. "jobs/category/it-software-job-vacancies"."""
    pages: dict[str, str] = {}
    for path in [p.strip().strip("/") for p in source.board.split("|") if p.strip()] or \
            ["jobs/category/it-software-job-vacancies"]:
        for n_page in range(FRESHERSWORLD_PAGES):
            time.sleep(DELAY)
            urls = item_list(get_text(FRESHERSWORLD.format(path=path), headers=BROWSER_HEADERS,
                                      params={"limit": 20, "offset": n_page * 20} if n_page else None))
            found = [(m.group(1), u) for u in urls if (m := _FW_ID_RE.search(urlsplit(u).path))]
            for ats_id, url in found:
                pages.setdefault(ats_id, url)
            if not found or all(ats_id in known for ats_id, _ in found):
                break
    jobs = read_pages(source, list(pages.items()), known, headers=BROWSER_HEADERS)
    # An Indian board: a job with no place given is somewhere in India.
    return [j.model_copy(update={"location_raw": "India", "india_eligible": "yes"}) if j.title and not j.location_raw
            else j for j in jobs]


# ---------------------------------------------------------------- any sitemap of job pages

_NUMERIC_ID_RE = re.compile(r"/(\d{3,})/?$")


def numbered_pages(urls: list[str]) -> list[tuple[str, str]]:
    return [(m.group(1), url) for url in urls if (m := _NUMERIC_ID_RE.search(urlsplit(url).path))]


def fetch_sitemap(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    urls = sitemap_urls(source.board, headers=BROWSER_HEADERS)
    return read_pages(source, numbered_pages(urls), known, headers=BROWSER_HEADERS)


FETCHERS = {
    "talentbrew": fetch_talentbrew,
    "icims": fetch_icims,
    "socgen": fetch_socgen,
    "cutshort": fetch_cutshort,
    "sitemap": fetch_sitemap,
    "freshersworld": fetch_freshersworld,
}


if __name__ == "__main__":
    kind, board = sys.argv[1], sys.argv[2]
    MAX_NEW = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    found = FETCHERS[kind](Source(name="test", type=kind, board=board))
    for j in found:
        print(f"{j.title[:50]:<50}  {j.location_raw[:35]:<35}  {j.work_mode:<7} {j.india_eligible:<7} "
              f"{j.posted_at and j.posted_at.date()}  {len(j.description)}")
    print(f"\n{len(found)} jobs on {board}")
