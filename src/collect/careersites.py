"""Big employers' own career sites, read through the calls their job search pages make.

Each exposes its own fetch(source, known). `board` is unused unless noted. All send a browser User-Agent.

- google: Google Careers (also DeepMind, YouTube, Waymo jobs); the search page embeds full jobs, 20 a page.
  google.com's robots.txt disallows these result pages; they are read on purpose, one a second.
- apple: jobs.apple.com; the search page embeds the jobs (title, team, summary), 20 a page.
- ibm: the search API behind ibm.com/careers; one call returns 100 jobs with a short summary.
- goldman: Goldman Sachs' job search (GraphQL), with full descriptions.
- sapsf: SAP SuccessFactors career sites (Wipro, HCLTech). `board` is the site URL plus its India filter,
  e.g. https://careers.wipro.com?location=India or https://careers.hcltech.com?custCountryRegion=India
  (any parameter other than `location` is a filter field). The list gives 10 jobs per call, newest
  first; each new job's page is opened once for its description (at most SAPSF_MAX_NEW per scan).
  The search API is under /services/, which these sites' robots.txt asks crawlers to avoid; it is
  used here on purpose, at one call a second.
- makemytrip: MakeMyTrip's career site API (its jobs live in Darwinbox); one extra call per new job.
"""

import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import httpx

from src import normalize as n
from src.collect.http import BROWSER_HEADERS, TIMEOUT, SourceError, get_json, get_text, post_json
from src.collect.jobpages import job_posting
from src.models import Job, Source

PAGE_DELAY = 1.0
DEFAULT_MAX_JOBS = 1000


def _job(source: Source, ats_id, title: str, place: str, description: str, url: str, posted: datetime | None,
         company: str | None = None, department: str | None = None, mode_value: str | None = None,
         remote: bool | None = None, exp: tuple[int | None, int | None] | None = None) -> Job:
    location = (place or "").strip(" ,|")
    description = n.html_to_text(description).strip()
    mode = n.work_mode(location, mode_value, is_remote=remote)
    exp_min, exp_max = exp if exp and exp != (None, None) else n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=str(ats_id),
        title=n.html_to_text(title).strip(),
        company=(company or "").strip() or source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=(department or "").strip() or None,
        description=description,
        apply_url=url,
        posted_at=posted,
    )


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _max_jobs(source: Source) -> int:
    return source.max_jobs or DEFAULT_MAX_JOBS


# ---------------------------------------------------------------- Google

GOOGLE_SEARCH = "https://www.google.com/about/careers/applications/jobs/results"
GOOGLE_JOB = "https://www.google.com/about/careers/applications/jobs/results/{id}"
_GOOGLE_DATA_RE = re.compile(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:(.*?), sideChannel: \{\}\}\);", re.S)


def google_page(page: str) -> tuple[list, int]:
    """(raw jobs, total jobs) from a search page."""
    m = _GOOGLE_DATA_RE.search(page)
    if not m:
        raise SourceError("bad_response", "no job data on the search page")
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        raise SourceError("bad_response", "job data is not JSON") from e
    return data[0] or [], int(data[2] or 0) if len(data) > 2 else 0


def _part(value, index: int = 1) -> str:
    return value[index] if isinstance(value, list) and len(value) > index and value[index] else ""


def google_job(raw: list, source: Source) -> Job:
    places = " | ".join(loc[0] for loc in raw[9] or [] if loc and loc[0])
    description = "".join(f"<div>{part}</div>" for part in
                          (_part(raw[10]), _part(raw[3]), _part(raw[4])) if part)
    created = raw[12][0] if isinstance(raw[12], list) and raw[12] else None
    return _job(source, raw[0], raw[1], places, description, GOOGLE_JOB.format(id=raw[0]),
                datetime.fromtimestamp(created, timezone.utc) if created else None, company=raw[7])


def fetch_google(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    page = 1
    while len(jobs) < _max_jobs(source):
        time.sleep(PAGE_DELAY)
        raws, total = google_page(get_text(GOOGLE_SEARCH, headers=BROWSER_HEADERS,
                                           params={"location": source.board or "India", "sort_by": "date", "page": page}))
        batch = [google_job(r, source) for r in raws if r and r[0]]
        for job in batch:
            jobs.setdefault(job.ats_id, job)
        if not batch or page * 20 >= total or all(j.ats_id in known for j in batch):
            break
        page += 1
    return list(jobs.values())


# ---------------------------------------------------------------- Apple

APPLE_SEARCH = "https://jobs.apple.com/en-in/search"
_APPLE_DATA_RE = re.compile(r'window\.__staticRouterHydrationData = JSON\.parse\("(.*?)"\);', re.S)


def apple_page(page: str) -> tuple[list[dict], int]:
    m = _APPLE_DATA_RE.search(page)
    if not m:
        raise SourceError("bad_response", "no job data on the search page")
    try:
        data = json.loads(json.loads(f'"{m.group(1)}"'))
    except ValueError as e:
        raise SourceError("bad_response", "job data is not JSON") from e
    search = (data.get("loaderData") or {}).get("search") or {}
    return search.get("searchResults") or [], int(search.get("totalRecords") or 0)


def apple_job(raw: dict, source: Source) -> Job:
    places = " | ".join(", ".join(x for x in (loc.get("name"), loc.get("countryName")) if x)
                        for loc in raw.get("locations") or [])
    position = raw.get("positionId") or raw["id"]
    url = f"https://jobs.apple.com/en-in/details/{position}/{raw.get('transformedPostingTitle') or 'job'}"
    return _job(source, raw["id"], raw.get("postingTitle") or "", places, raw.get("jobSummary") or "", url,
                _iso(raw.get("postDateInGMT")), department=(raw.get("team") or {}).get("teamName"),
                remote=bool(raw.get("homeOffice")) or None)


def fetch_apple(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    page = 1
    while len(jobs) < _max_jobs(source):
        time.sleep(PAGE_DELAY)
        raws, total = apple_page(get_text(APPLE_SEARCH, headers=BROWSER_HEADERS, params={
            "location": source.board or "india-INDC", "sort": "newest", "page": page}))
        batch = [apple_job(r, source) for r in raws if r.get("id")]
        for job in batch:
            jobs.setdefault(job.ats_id, job)
        if not batch or page * 20 >= total or all(j.ats_id in known for j in batch):
            break
        page += 1
    return list(jobs.values())


# ---------------------------------------------------------------- IBM

IBM_API = "https://www-api.ibm.com/search/api/v2"
IBM_PAGE_SIZE = 100
IBM_FIELDS = ["title", "url", "description", "dcdate", "field_keyword_05", "field_keyword_08",
              "field_keyword_17", "field_keyword_18", "field_keyword_19", "field_text_01"]


def ibm_job(raw: dict, source: Source) -> Job:
    s = raw.get("_source") or {}
    country = s.get("field_keyword_05") or ""
    place = re.sub(r",\s*IN$", ", India", (s.get("field_keyword_19") or "").strip(), flags=re.I)  # "GURGAON, IN"
    place = ", ".join(w.strip().title() if w.strip().isupper() else w.strip() for w in place.split(","))
    if country and country.lower() not in place.lower():
        place = f"{place}, {country}" if place else country                     # "Multiple Cities" -> "..., India"
    level = s.get("field_keyword_18") or ""
    return _job(source, s.get("field_text_01") or raw["_id"], s.get("title") or "", place,
                s.get("description") or "", s["url"], _iso(s.get("dcdate")),
                department=", ".join(x for x in (s.get("field_keyword_08"), level) if x),
                mode_value=s.get("field_keyword_17"))


def fetch_ibm(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    offset = 0
    while offset < _max_jobs(source):
        time.sleep(PAGE_DELAY)
        data = post_json(IBM_API, headers=BROWSER_HEADERS, body={
            "appId": "careers", "scopes": ["careers2"], "query": {"bool": {"must": []}},
            "post_filter": {"term": {"field_keyword_05": source.board or "India"}},
            "size": IBM_PAGE_SIZE, "from": offset, "sort": [{"dcdate": "desc"}], "lang": "zz",
            "localeSelector": {}, "sm": {"query": "", "lang": "zz"}, "_source": IBM_FIELDS})
        hits = (data.get("hits") or {}).get("hits") or []
        batch = [ibm_job(h, source) for h in hits if (h.get("_source") or {}).get("url")]
        for job in batch:
            jobs.setdefault(job.ats_id, job)
        offset += IBM_PAGE_SIZE
        total = ((data.get("hits") or {}).get("total") or {}).get("value") or 0
        if not batch or offset >= total or all(j.ats_id in known for j in batch):
            break
    return list(jobs.values())


# ---------------------------------------------------------------- Goldman Sachs

GOLDMAN_API = "https://api-higher.gs.com/gateway/api/v1/graphql"
GOLDMAN_PAGE_SIZE = 50
GOLDMAN_QUERY = """query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items { roleId jobTitle corporateTitle division descriptionHtml
            locations { primary state country city } externalSource { sourceId } }
  }
}"""


def goldman_job(raw: dict, source: Source) -> Job:
    places = " | ".join(", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x)
                        for loc in raw.get("locations") or [])
    ref = (raw.get("externalSource") or {}).get("sourceId") or raw["roleId"].split("_")[0]
    title = raw.get("jobTitle") or ""
    if raw.get("corporateTitle") and raw["corporateTitle"].lower() not in title.lower():
        title = f"{title} ({raw['corporateTitle']})"
    return _job(source, raw["roleId"], title, places, raw.get("descriptionHtml") or "",
                f"https://higher.gs.com/roles/{ref}", None, department=raw.get("division"))


def fetch_goldman(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    page = 0
    while len(jobs) < _max_jobs(source):
        time.sleep(PAGE_DELAY)
        data = post_json(GOLDMAN_API, headers=BROWSER_HEADERS, body={
            "operationName": "GetRoles", "query": GOLDMAN_QUERY, "variables": {"searchQueryInput": {
                "page": {"pageSize": GOLDMAN_PAGE_SIZE, "pageNumber": page},
                "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
                "filters": [{"filterCategoryType": "LOCATION",
                             "filters": [{"filter": source.board or "India", "subFilters": []}]}],
                "experiences": ["EARLY_CAREER", "PROFESSIONAL"], "searchTerm": ""}}})
        if data.get("errors"):
            raise SourceError("bad_response", str(data["errors"])[:200])
        found = ((data.get("data") or {}).get("roleSearch")) or {}
        batch = [goldman_job(r, source) for r in found.get("items") or [] if r.get("roleId")]
        for job in batch:
            jobs.setdefault(job.ats_id, job)
        page += 1
        if not batch or page * GOLDMAN_PAGE_SIZE >= (found.get("totalCount") or 0):
            break
    return list(jobs.values())


# ---------------------------------------------------------------- SAP SuccessFactors (Wipro, HCLTech)

SAPSF_MAX_NEW = 150     # job pages opened per scan
_CSRF_RE = re.compile(r'var CSRFToken\s*=\s*"([^"]+)"')
_MICRODATA_RE = re.compile(r'itemprop="description"[^>]*>(.*?)</span>\s*</div>', re.S)


def sapsf_board(board: str) -> tuple[str, str, dict[str, list[str]]]:
    """(site URL, location words, filter fields) from a board like https://careers.wipro.com?location=India."""
    u = urlsplit(board if "://" in board else "https://" + board)
    params = dict(parse_qsl(u.query))
    location = params.pop("location", "")
    return f"{u.scheme}://{u.netloc}", location, {k: [v] for k, v in params.items()}


class _Site:
    """One visit to a SuccessFactors site: its search API needs the session cookie and CSRF token."""

    def __init__(self, board: str):
        self.base, self.location, self.facets = sapsf_board(board)
        self.client = httpx.Client(headers=BROWSER_HEADERS, timeout=TIMEOUT, follow_redirects=True)
        token = _CSRF_RE.search(self._send("GET", f"{self.base}/search/", params={"q": ""}).text)
        if not token:
            raise SourceError("bad_response", "no CSRF token on the search page")
        self.token = token.group(1)

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            resp = self.client.request(method, url, **kw)
        except httpx.TimeoutException as e:
            raise SourceError("timeout", str(e)) from e
        except httpx.HTTPError as e:
            raise SourceError("network", str(e)) from e
        if resp.status_code in (401, 403):
            raise SourceError("blocked", f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            raise SourceError("not_found", url)
        if resp.status_code != 200:
            raise SourceError("http", f"HTTP {resp.status_code}")
        return resp

    def jobs(self, page: int) -> tuple[list[dict], int]:
        body = {"locale": "en_US", "pageNumber": page, "sortBy": "date", "keywords": "", "location": self.location,
                "facetFilters": self.facets, "brand": "", "skills": [], "categoryId": 0, "alertId": "",
                "rcmCandidateId": ""}
        resp = self._send("POST", f"{self.base}/services/recruiting/v1/jobs", json=body,
                          headers={"X-CSRF-Token": self.token, "Accept": "application/json"})
        try:
            data = resp.json()
        except ValueError as e:
            raise SourceError("bad_response", "not JSON") from e
        return [r.get("response") or {} for r in data.get("jobSearchResult") or []], int(data.get("totalJobs") or 0)


def _first(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value:
            return re.sub(r"<br\s*/?>", "", str(value)).strip()
    return ""


def sapsf_place(raw: dict) -> str:
    short = _first(raw, "jobLocationShort")
    if short:      # "Hyderabad, IND-36, IND, 500032"
        city = short.split(",")[0].strip()
        country = _first(raw, "jobLocationCountry") or ("India" if ", IND" in short else "")
        return ", ".join(x for x in (city, _first(raw, "jobLocationState"), country) if x)
    city = _first(raw, "custprimecity", "sfstd_jobLocation_obj", "jobLocationCity", "city")
    return ", ".join(x for x in (city, _first(raw, "jobLocationState"), _first(raw, "jobLocationCountry", "custCountryRegion")) if x)


def sapsf_url(base: str, raw: dict) -> str:
    return f"{base}/job/{raw.get('urlTitle') or raw.get('unifiedUrlTitle') or 'job'}/{raw['id']}-en_US/"


def sapsf_description(page: str) -> str:
    posting = job_posting(page)
    if posting and posting.get("description"):
        return str(posting["description"])
    return "".join(f"<div>{part}</div>" for part in _MICRODATA_RE.findall(page))


def sapsf_job(raw: dict, base: str, source: Source, description: str = "") -> Job:
    try:
        posted = datetime.strptime(raw.get("unifiedStandardStart") or "", "%m/%d/%y").replace(tzinfo=timezone.utc)
    except ValueError:
        posted = None
    return _job(source, raw["id"], raw.get("unifiedStandardTitle") or raw.get("title") or "", sapsf_place(raw),
                description, sapsf_url(base, raw), posted,
                department=_first(raw, "custRMKMappingPicklist", "department", "custDepartment"))


def fetch_sapsf(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    site = _Site(source.board)
    raws: dict[str, dict] = {}
    page = 0
    while len(raws) < _max_jobs(source):
        time.sleep(PAGE_DELAY)
        items, total = site.jobs(page)
        items = [r for r in items if r.get("id")]
        for raw in items:
            raws.setdefault(str(raw["id"]), raw)
        page += 1
        if not items or page * 10 >= total or all(str(r["id"]) in known for r in items):
            break
    jobs, opened = [], 0
    for job_id, raw in raws.items():
        if job_id in known:
            jobs.append(sapsf_job(raw, site.base, source))
            continue
        if opened >= SAPSF_MAX_NEW:
            continue    # left for the next scan; a job saved without its description would stay blank
        time.sleep(PAGE_DELAY)
        opened += 1
        try:
            page_html = get_text(sapsf_url(site.base, raw), headers=BROWSER_HEADERS)
        except SourceError as e:
            if e.reason == "not_found":
                continue
            raise
        jobs.append(sapsf_job(raw, site.base, source, sapsf_description(page_html)))
    return jobs


# ---------------------------------------------------------------- MakeMyTrip

MMT_API = "https://careers.makemytrip.com/api"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "job"


def _mmt_date(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", "%d-%m-%Y %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def mmt_job(raw: dict, source: Source, detail: dict | None = None) -> Job:
    d = detail or {}
    places = " | ".join(raw.get("location") or []) or raw.get("location_country") or ""
    places = re.sub(r"\s*\([^)]*\)", "", places)         # "Bangalore, Karnataka, India (Bangalore_MMT)"
    description = html.unescape(d.get("job_decription") or d.get("job_description") or "")
    def years(key):
        return int(raw[key]) if str(raw.get(key) or "").isdigit() else None
    return _job(source, raw["job_id"], raw.get("job_title") or "", places, description,
                f"https://careers.makemytrip.com/opportunity/{raw['job_id']}/{_slug(raw.get('job_title') or '')}",
                _mmt_date(raw.get("job_created_timestamp")), company=raw.get("group_company"),
                department=raw.get("department"), remote=bool(raw.get("is_remote")) or None,
                exp=(years("experience_from"), years("experience_to")))


def fetch_makemytrip(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    data = get_json(f"{MMT_API}/jobs", headers=BROWSER_HEADERS)
    jobs = []
    for raw in data.get("allJobs") or []:
        if not raw.get("job_id") or not raw.get("post_on_careers_page", 1):
            continue
        if raw["job_id"] in known:
            jobs.append(mmt_job(raw, source))
            continue
        time.sleep(PAGE_DELAY)
        detail = get_json(f"{MMT_API}/jobDetails", params={"jobId": raw["job_id"]}, headers=BROWSER_HEADERS)
        jobs.append(mmt_job(raw, source, detail.get("data") or {}))
    return jobs


FETCHERS = {
    "google": fetch_google,
    "apple": fetch_apple,
    "ibm": fetch_ibm,
    "goldman": fetch_goldman,
    "sapsf": fetch_sapsf,
    "makemytrip": fetch_makemytrip,
}


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "google"
    found = FETCHERS[kind](Source(name=kind, type=kind, board=sys.argv[2] if len(sys.argv) > 2 else "",
                                  max_jobs=int(sys.argv[3]) if len(sys.argv) > 3 else None))
    for j in found:
        print(f"{j.company[:18]:<18}  {j.title[:48]:<48}  {j.location_raw[:30]:<30}  {j.india_eligible:<7}  "
              f"{j.posted_at.date() if j.posted_at else '-'}  {len(j.description)}")
    print(f"\n{len(found)} jobs on {kind}")
