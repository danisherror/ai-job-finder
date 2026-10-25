"""Job portals read through the same calls their own web pages make (no API key, no login).

Each portal exposes its own fetch(source, known). They all send a normal browser User-Agent, because
most of these sites refuse unknown ones. `board` holds search keywords (or page names), several
separated by "|"; each one is a separate search. Every portal here is a job board listing many
companies, so `company` is the real employer.

- linkedin: LinkedIn's public job search (the pages shown to people who are not logged in). The list
  has no description, so each new job costs one more call (at most LINKEDIN_MAX_NEW per scan).
  LinkedIn's robots.txt disallows these pages; they are read on purpose, one call every 2 seconds, and
  the scan stops opening job pages at the first HTTP 429.
- hirist, iimjobs: the same job system; the list carries skills and experience but no description.
- instahyre: the list has no description, date or sort order, so each new job's page is opened once,
  highest ID (newest) first. Many listed jobs are old (the page gives their first posting date).
- unstop: jobs and internships with full descriptions. `board` is "jobs", "internships" or both.
- foundit (formerly Monster India), shine, timesjobs: search APIs with skills and a short snippet.
  Foundit's API is under /middleware/, which its robots.txt disallows; it is read on purpose.
- ambitionbox: AmbitionBox's job lists, which carry Naukri's jobs. Only the first page of each list is
  readable (later pages show a bot check), so `board` lists many keyword pages, e.g. "java-jobs-ski".
- wellfound: the India role pages, e.g. "software-engineer" for wellfound.com/role/l/software-engineer/india.
- internshala: list pages, e.g. "internships/computer-science-internship" or "jobs/software-development-jobs".
- themuse: The Muse's public API; `board` holds places, e.g. "Bengaluru, India".
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from src import normalize as n
from src.collect.http import BROWSER_HEADERS, SourceError, get_json, get_text, post_json
from src.collect.jobpages import job_posting
from src.models import Job, Source

MAX_AGE_DAYS = 14
PAGE_DELAY = 1.0


def _words(source: Source, default: str) -> list[str]:
    return [k.strip() for k in source.board.split("|") if k.strip()] or [default]


def _in_india(place: str) -> str:
    """These searches are limited to India, so a place without a country is in India (remote jobs too:
    an Indian portal's "Remote" means remote within India)."""
    place = (place or "").strip(" ,|")
    if not place:
        return "India"
    return place if n.mentions_india(place) else f"{place}, India"


def _ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_AGO_RE = re.compile(r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month)s?\b", re.I)


def ago(text: str | None, now: datetime | None = None) -> datetime | None:
    """'3 days ago', '1 week ago', 'Few hours ago', 'Today' -> a date; None if not understood."""
    now = now or datetime.now(timezone.utc)
    low = (text or "").strip().lower()
    if not low:
        return None
    if low in ("today", "just now", "few hours ago", "a few hours ago") or "hour" in low and not _AGO_RE.search(low):
        return now
    if low == "yesterday":
        return now - timedelta(days=1)
    m = _AGO_RE.search(low)
    if not m:
        return None
    count, unit = int(m.group(1)), m.group(2)
    days = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30}[unit] * count
    return now - timedelta(days=days)


def _job(source: Source, ats_id, title: str, company: str, place: str, description: str, url: str,
         posted: datetime | None, salary: str | None = None, department: str | None = None,
         exp: tuple[int | None, int | None] | None = None, remote: bool | None = None,
         in_india: bool = True) -> Job:
    location = _in_india(place) if in_india else (place or "").strip(" ,|")
    description = n.html_to_text(description).strip()
    mode = n.work_mode(f"{location} {title}", is_remote=remote)
    exp_min, exp_max = exp if exp and exp != (None, None) else n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=str(ats_id),
        title=n.html_to_text(title).strip(),
        company=n.html_to_text(company).strip() or "Unknown",
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        salary=(salary or "").strip() or None,
        department=(department or "").strip() or None,
        description=description,
        apply_url=url,
        posted_at=posted,
    )


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _exp(lo, hi) -> tuple[int | None, int | None]:
    lo, hi = _int(lo), _int(hi)
    return (lo, hi if hi is None or lo is None or hi >= lo else None) if lo is not None or hi is not None else (None, None)


def _fresh(job: Job, cutoff: datetime) -> bool:
    return job.posted_at is None or job.posted_at >= cutoff


def _cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)


# ---------------------------------------------------------------- LinkedIn

LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_JOB = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
LINKEDIN_PAGE_SIZE = 10
LINKEDIN_MAX_PAGES = 10      # per keyword: the newest 100 of the past week
LINKEDIN_MAX_NEW = 150       # job pages opened per scan; the rest wait for the next scan
LINKEDIN_DELAY = 2.0
_LI_CARD_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)</li>', re.S)
_LI_TITLE_RE = re.compile(r'class="base-search-card__title"[^>]*>(.*?)</h3>', re.S)
_LI_COMPANY_RE = re.compile(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', re.S)
_LI_PLACE_RE = re.compile(r'class="job-search-card__location"[^>]*>(.*?)</span>', re.S)
_LI_DATE_RE = re.compile(r'<time[^>]*datetime="([\d-]+)"')
_LI_DESC_RE = re.compile(r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.S)
_LI_CRITERIA_RE = re.compile(r'description__job-criteria-subheader"[^>]*>(.*?)</h3>\s*<span[^>]*>(.*?)</span>', re.S)


def linkedin_cards(page: str) -> list[dict]:
    cards = []
    for job_id, body in _LI_CARD_RE.findall(page):
        title, company = _LI_TITLE_RE.search(body), _LI_COMPANY_RE.search(body)
        place, date = _LI_PLACE_RE.search(body), _LI_DATE_RE.search(body)
        if title:
            cards.append({"id": job_id, "title": n.html_to_text(title.group(1)),
                          "company": n.html_to_text(company.group(1)) if company else "",
                          "place": n.html_to_text(place.group(1)) if place else "",
                          "date": date.group(1) if date else None})
    return cards


def linkedin_details(page: str) -> tuple[str, dict[str, str]]:
    """The description and the criteria list (seniority, employment type, function, industry)."""
    desc = _LI_DESC_RE.search(page)
    criteria = {n.html_to_text(k): n.html_to_text(v) for k, v in _LI_CRITERIA_RE.findall(page)}
    return (desc.group(1) if desc else ""), criteria


def linkedin_job(card: dict, source: Source, page: str | None = None) -> Job:
    description, criteria = linkedin_details(page) if page else ("", {})
    extra = " | ".join(f"{k}: {v}" for k, v in criteria.items() if v)
    return _job(source, card["id"], card["title"], card["company"], card["place"],
                f"{description}<p>{extra}</p>" if extra else description,
                f"https://www.linkedin.com/jobs/view/{card['id']}", _iso(card.get("date")),
                department=criteria.get("Job function"))


def fetch_linkedin(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    cards: dict[str, dict] = {}
    for keyword in _words(source, "software engineer"):
        for page in range(LINKEDIN_MAX_PAGES):
            time.sleep(LINKEDIN_DELAY)
            found = linkedin_cards(get_text(LINKEDIN_SEARCH, headers=BROWSER_HEADERS, params={
                "keywords": keyword, "location": "India", "f_TPR": "r604800", "sortBy": "DD",
                "start": page * LINKEDIN_PAGE_SIZE}))
            for card in found:
                cards.setdefault(card["id"], card)
            if len(found) < LINKEDIN_PAGE_SIZE or all(c["id"] in known for c in found):
                break
    jobs, opened = [], 0
    for card in cards.values():
        if card["id"] in known:
            jobs.append(linkedin_job(card, source))
            continue
        if opened >= LINKEDIN_MAX_NEW:
            continue    # left for the next scan; a job saved without its description would stay blank
        time.sleep(LINKEDIN_DELAY)
        opened += 1
        try:
            page = get_text(LINKEDIN_JOB.format(id=card["id"]), headers=BROWSER_HEADERS, rate_limit_retries=1)
        except SourceError as e:
            if e.reason == "not_found":
                continue
            if e.reason in ("rate_limited", "blocked"):
                break   # LinkedIn asked us to slow down; keep what we have and try the rest next scan
            raise
        jobs.append(linkedin_job(card, source, page))
    return jobs


# ---------------------------------------------------------------- Hirist and iimjobs

HIRIST_API = {"hirist": "https://gladiator.hirist.tech/job/search",
              "iimjobs": "https://gladiator.iimjobs.com/job/search"}
HIRIST_PAGE_SIZE = 50
HIRIST_MAX_PAGES = 4


def hirist_job(raw: dict, source: Source) -> Job:
    places = [p.get("name", "") for p in raw.get("locations") or []] + [raw.get("otherLocation") or ""]
    skills = ", ".join(t.get("name", "") for t in raw.get("tags") or [])
    company = (raw.get("companyData") or {}).get("companyName") or ""
    return _job(source, raw["id"], raw["title"], company, ", ".join(p for p in places if p),
                f"Skills: {skills}" if skills else "", raw["jobDetailUrl"], _ms(raw.get("createdTimeMs") or raw.get("createdTime")),
                exp=_exp(raw.get("min"), raw.get("max")), remote=bool(raw.get("workFromHome")) or None)


def fetch_hirist(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    api = HIRIST_API[source.type]
    cutoff = _cutoff()
    jobs: dict[str, Job] = {}
    for keyword in _words(source, "software"):
        for page in range(HIRIST_MAX_PAGES):
            time.sleep(PAGE_DELAY)
            data = get_json(api, params={"pageNo": page, "query": keyword}, headers=BROWSER_HEADERS)
            batch = [hirist_job(r, source) for r in data.get("data") or [] if r.get("jobDetailUrl")]
            fresh = [j for j in batch if _fresh(j, cutoff)]
            for job in fresh:
                jobs.setdefault(job.ats_id, job)
            if (not data.get("hasMore") or len(fresh) < len(batch) or not batch
                    or all(j.ats_id in known for j in batch)):
                break
    return list(jobs.values())


# ---------------------------------------------------------------- Instahyre

INSTAHYRE_API = "https://www.instahyre.com/api/v1/job_search"
INSTAHYRE_PAGE_SIZE = 20     # the API's maximum
INSTAHYRE_MAX_PAGES = 15
INSTAHYRE_MAX_NEW = 100      # job pages opened per scan


def instahyre_job(raw: dict, source: Source, posting: dict | None = None) -> Job:
    p = posting or {}
    skills = ", ".join(raw.get("keywords") or [])
    description = p.get("description") or ""
    return _job(source, raw["id"], raw.get("title") or p.get("title") or "",
                (raw.get("employer") or {}).get("company_name") or "", raw.get("locations") or "",
                f"{description}<p>Skills: {skills}</p>" if skills and posting else description,
                raw["public_url"], _iso(p.get("datePosted")))


def fetch_instahyre(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    """`board` is empty (every job) or job-function IDs from the site's filters, e.g. "10|1"."""
    functions = [w for w in source.board.split("|") if w.strip()] or [""]
    raws: dict[str, dict] = {}
    for function in functions:
        params = {"company_size": 0, "job_type": 0, "isLandingPage": "true", "limit": INSTAHYRE_PAGE_SIZE}
        if function:
            params["job_functions"] = function.strip()
        for page in range(INSTAHYRE_MAX_PAGES):
            time.sleep(PAGE_DELAY)
            data = get_json(INSTAHYRE_API, params={**params, "offset": page * INSTAHYRE_PAGE_SIZE},
                            headers=BROWSER_HEADERS)
            items = [r for r in data.get("objects") or [] if r.get("public_url")]
            for raw in items:
                raws.setdefault(str(raw["id"]), raw)
            if not (data.get("meta") or {}).get("next") or not items or all(str(r["id"]) in known for r in items):
                break
    jobs, opened = [], 0
    # The list has no date and no sort option, but IDs grow over time, so the newest are opened first.
    for job_id, raw in sorted(raws.items(), key=lambda kv: -int(kv[0])):
        if job_id in known:
            jobs.append(instahyre_job(raw, source))
            continue
        if opened >= INSTAHYRE_MAX_NEW:
            continue
        time.sleep(PAGE_DELAY)
        opened += 1
        try:
            posting = job_posting(get_text(raw["public_url"], headers=BROWSER_HEADERS))
        except SourceError as e:
            if e.reason == "not_found":
                continue
            raise
        if posting:
            jobs.append(instahyre_job(raw, source, posting))
    return jobs


# ---------------------------------------------------------------- Unstop

UNSTOP_API = "https://unstop.com/api/public/opportunity/search-result"
UNSTOP_PAGE_SIZE = 50
UNSTOP_MAX_PAGES = 6


def unstop_job(raw: dict, source: Source) -> Job:
    detail = raw.get("jobDetail") or {}
    places = []
    for loc in detail.get("locations") or []:
        places.append(loc.get("city") or loc.get("state") or "" if isinstance(loc, dict) else str(loc))
    address = raw.get("address_with_country_logo") or {}
    places.append(", ".join(x for x in (address.get("city"), address.get("state"), address.get("country")) if x))
    kind = str(detail.get("type") or "").lower()
    remote = kind in ("wfh", "remote", "work_from_home") or None
    place = ", ".join(dict.fromkeys(p for p in places if p)) or ("Remote" if remote else "")
    if kind == "hybrid":
        place = f"{place} (Hybrid)" if place else "Hybrid"
    skills = ", ".join(s.get("skill_name") or s.get("skill") or "" for s in raw.get("required_skills") or [])
    salary = None
    if detail.get("show_salary") and (detail.get("min_salary") or detail.get("max_salary")):
        salary = f"INR {detail.get('min_salary') or ''}-{detail.get('max_salary') or ''} {detail.get('salary_type') or ''}"
    description = raw.get("details") or ""
    if skills:
        description += f"<p>Skills: {skills}</p>"
    posted = _iso(raw.get("approved_date", "")[:19].replace(" ", "T") if raw.get("approved_date") else None) \
        or _iso(raw.get("updated_at"))
    return _job(source, raw["id"], raw["title"], (raw.get("organisation") or {}).get("name") or "", place,
                description, raw.get("seo_url") or f"https://unstop.com/{raw['public_url']}", posted,
                salary=salary, department=raw.get("type"),
                exp=_exp(detail.get("min_experience"), detail.get("max_experience")), remote=remote)


def fetch_unstop(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    for kind in _words(source, "jobs"):
        for page in range(1, UNSTOP_MAX_PAGES + 1):
            time.sleep(PAGE_DELAY)
            data = (get_json(UNSTOP_API, params={"opportunity": kind, "per_page": UNSTOP_PAGE_SIZE, "page": page,
                                                  "oppstatus": "open"}, headers=BROWSER_HEADERS).get("data") or {})
            items = data.get("data") or []
            for raw in items:
                jobs.setdefault(str(raw["id"]), unstop_job(raw, source))
            if page >= (data.get("last_page") or 0) or len(items) < UNSTOP_PAGE_SIZE \
                    or all(str(r["id"]) in known for r in items):
                break
    return list(jobs.values())


# ---------------------------------------------------------------- Foundit

FOUNDIT_API = "https://www.foundit.in/middleware/jobsearch"
FOUNDIT_HEADERS = {**BROWSER_HEADERS, "Accept": "application/json", "Referer": "https://www.foundit.in/"}
FOUNDIT_PAGE_SIZE = 50
FOUNDIT_MAX_PAGES = 6


def foundit_job(raw: dict, source: Source) -> Job:
    exp = _exp((raw.get("minimumExperience") or {}).get("years"), (raw.get("maximumExperience") or {}).get("years"))
    skills = raw.get("skills") or ""
    url = "https://www.foundit.in" + (raw.get("seoJdUrl") or raw.get("jdUrl") or f"/job/{raw['jobId']}")
    sal = raw.get("salary") or ""
    salary = None if raw.get("hideSalary") or sal.startswith("0-0") or not sal else sal
    return _job(source, raw["jobId"], raw["title"], raw.get("companyName") or "", raw.get("locations") or "",
                f"Skills: {skills}" if skills else "", url, _ms(raw.get("createdAt")), salary=salary,
                department=", ".join(raw.get("functions") or []) if isinstance(raw.get("functions"), list) else None,
                exp=exp)


def fetch_foundit(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    cutoff = _cutoff()
    jobs: dict[str, Job] = {}
    for keyword in _words(source, "software engineer"):
        for page in range(FOUNDIT_MAX_PAGES):
            time.sleep(PAGE_DELAY)
            data = get_json(FOUNDIT_API, headers=FOUNDIT_HEADERS, params={
                "start": page * FOUNDIT_PAGE_SIZE, "limit": FOUNDIT_PAGE_SIZE, "query": keyword, "sort": 1})
            items = (data.get("jobSearchResponse") or {}).get("data") or []
            batch = [foundit_job(r, source) for r in items if r.get("jobId")]
            fresh = [j for j in batch if _fresh(j, cutoff)]
            for job in fresh:
                jobs.setdefault(job.ats_id, job)
            if len(batch) < FOUNDIT_PAGE_SIZE or len(fresh) < len(batch) or all(j.ats_id in known for j in batch):
                break
    return list(jobs.values())


# ---------------------------------------------------------------- Shine

SHINE_API = "https://www.shine.com/api/v2/search/simple/"
SHINE_MAX_PAGES = 5           # 20 jobs a page. Sorting by date loosens the match (sales jobs for
                              # "software engineer"), so the list is read in its own order instead.


def _shine_places(value) -> str:
    if isinstance(value, list):
        return ", ".join(value)
    return ", ".join(p.strip(" '\"") for p in str(value or "").strip("[]").split(",") if p.strip(" '\""))


def shine_job(raw: dict, source: Source) -> Job:
    place = _shine_places(raw.get("jLoc"))
    if place.lower() in ("all india", "anywhere in india"):
        place = "India"
    exp = re.match(r"\s*(\d+)\s*to\s*(\d+)", raw.get("jExp") or "")
    skills = raw.get("jKwd") or ""
    description = (raw.get("jJD") or "") + (f"<p>Skills: {skills}</p>" if skills else "")
    return _job(source, raw["id"], raw["jJT"], raw.get("jCName") or "", place, description,
                f"https://www.shine.com/jobs/{raw['jSlug']}", _iso(raw.get("jPDate")), department=raw.get("jInd"),
                exp=_exp(exp.group(1), exp.group(2)) if exp else None)


def fetch_shine(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    cutoff = _cutoff()
    jobs: dict[str, Job] = {}
    for keyword in _words(source, "software engineer"):
        for page in range(1, SHINE_MAX_PAGES + 1):
            time.sleep(PAGE_DELAY)
            data = get_json(SHINE_API, params={"q": keyword, "page": page}, headers=BROWSER_HEADERS)
            batch = [shine_job(r, source) for r in data.get("results") or [] if r.get("jSlug")]
            for job in batch:
                if _fresh(job, cutoff):
                    jobs.setdefault(job.ats_id, job)
            if not data.get("next") or not batch:
                break
    return list(jobs.values())


# ---------------------------------------------------------------- TimesJobs

TIMESJOBS_API = "https://tjapi.timesjobs.com/search/api/v1/search/jobs/list"
TIMESJOBS_PAGE_SIZE = 100
TIMESJOBS_MAX_PAGES = 3
# TimesJobs also lists jobs abroad. Its place filter takes a list of names; "India" alone matches only
# jobs whose place is the word India, so the main cities are named too.
TIMESJOBS_PLACES = ("India,Bengaluru,Hyderabad,Pune,Chennai,Mumbai,Noida,Gurgaon,Gurugram,Delhi,Kolkata,"
                    "Ahmedabad,Kochi,Coimbatore,Jaipur,Indore,Chandigarh,Thiruvananthapuram")


_TJ_TITLE_TAIL = re.compile(r"(\s+Job)?\s+Job Details\s*\|.*$", re.S)   # "Java Developer Job Job Details | Yash..."


def timesjobs_job(raw: dict, source: Source) -> Job:
    lo, hi = _int(raw.get("lowSalary")), _int(raw.get("highSalary"))
    salary = f"INR {lo:,}-{hi:,} yearly" if lo and hi and lo > 0 else None
    skills = raw.get("skills") or ""
    place = raw.get("location") or ""
    mode = (raw.get("jobType") or "").lower()
    if "hybrid" in mode:
        place += " (Hybrid)"
    title = _TJ_TITLE_TAIL.sub("", raw["title"])
    return _job(source, raw["jobId"], title, raw.get("company") or raw.get("hfCompany") or "", place,
                (raw.get("description") or "") + (f"<p>Skills: {skills}</p>" if skills else ""),
                raw["jobDetailUrl"], _iso(raw.get("postDate")), salary=salary, department=raw.get("jobFunction"),
                exp=_exp(raw.get("experienceFrom"), raw.get("experienceTo")),
                remote="remote" in mode or "home" in mode or None)


def fetch_timesjobs(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    cutoff = _cutoff()
    jobs: dict[str, Job] = {}
    for keyword in _words(source, "software developer"):
        for page in range(1, TIMESJOBS_MAX_PAGES + 1):
            time.sleep(PAGE_DELAY)
            data = post_json(TIMESJOBS_API, {"keyword": keyword, "page": page, "size": TIMESJOBS_PAGE_SIZE,
                                             "location": TIMESJOBS_PLACES}, headers=BROWSER_HEADERS)
            batch = [timesjobs_job(r, source) for r in data.get("jobs") or [] if r.get("jobDetailUrl")]
            fresh = [j for j in batch if _fresh(j, cutoff)]
            for job in fresh:
                jobs.setdefault(job.ats_id, job)
            if page >= (data.get("totalPages") or 0) or not batch or all(j.ats_id in known for j in batch):
                break
    return list(jobs.values())


# ---------------------------------------------------------------- AmbitionBox (Naukri's jobs)

AMBITIONBOX_PAGE = "https://www.ambitionbox.com/jobs/{slug}"
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(page: str) -> dict:
    """The page data a Next.js site embeds for its first render."""
    m = _NEXT_RE.search(page)
    if not m:
        raise SourceError("bad_response", "no page data")
    try:
        return json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    except ValueError as e:
        raise SourceError("bad_response", "page data is not JSON") from e


def ambitionbox_job(raw: dict, source: Source) -> Job:
    skills = ", ".join(raw.get("skills") or [])
    lo, hi = raw.get("minCtc"), raw.get("maxCtc")
    salary = f"INR {int(lo):,}-{int(hi):,} yearly" if not raw.get("hideCtc") and lo and hi else None
    return _job(source, raw["jobId"], raw["title"], raw.get("company") or "", ", ".join(raw.get("locations") or []),
                f"Skills: {skills}" if skills else "", "https://www.ambitionbox.com" + raw["jdpUrl"],
                ago(raw.get("postedOn")), salary=salary, exp=_exp(raw.get("minExp"), raw.get("maxExp")))


def fetch_ambitionbox(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    lists = _words(source, "software-engineer-jobs-prf")
    for slug in lists:
        time.sleep(PAGE_DELAY)
        try:
            page = get_text(AMBITIONBOX_PAGE.format(slug=slug.strip("/")), params={"sortBy": "latest"},
                            headers=BROWSER_HEADERS)
        except SourceError as e:
            if e.reason == "not_found":
                continue    # a list name that no longer exists; the others still count
            raise
        if "<title>Verify - AmbitionBox" in page:
            if jobs:
                break    # the bot check came up part way; keep what we have
            raise SourceError("blocked", "bot check")
        for raw in next_data(page).get("jobsList") or []:
            if raw.get("jobId") and raw.get("jdpUrl"):
                jobs.setdefault(raw["jobId"], ambitionbox_job(raw, source))
    return list(jobs.values())


# ---------------------------------------------------------------- Wellfound

WELLFOUND_PAGE = "https://wellfound.com/role/l/{role}/india"
WELLFOUND_MAX_PAGES = 3


def wellfound_jobs(page: str, source: Source) -> list[Job]:
    data = ((next_data(page).get("apolloState") or {}).get("data")) or {}
    company = {}
    for key, value in data.items():
        if key.startswith("StartupResult"):
            for ref in value.get("highlightedJobListings") or []:
                company[ref.get("__ref")] = value.get("name") or ""
    jobs = []
    for key, raw in data.items():
        if not key.startswith("JobListingSearchResult") or not raw.get("id"):
            continue
        places = list(raw.get("locationNames") or [])
        if raw.get("remote"):
            accepted = raw.get("acceptedRemoteLocationNames") or []
            places.append("Remote" + (f" ({', '.join(accepted)})" if accepted else ""))
        exp = _exp(raw.get("yearsExperienceMin"), raw.get("yearsExperienceMax"))
        jobs.append(_job(source, raw["id"], raw.get("title") or "", company.get(key, ""), " | ".join(places),
                         raw.get("description") or "", f"https://wellfound.com/jobs/{raw['id']}-{raw.get('slug') or 'job'}",
                         datetime.fromtimestamp(raw["liveStartAt"], timezone.utc) if raw.get("liveStartAt") else None,
                         salary=raw.get("compensation"), department=raw.get("jobType"), exp=exp,
                         remote=bool(raw.get("remote")) or None, in_india=False))
    return jobs


def fetch_wellfound(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    """Wellfound's India pages also list jobs elsewhere at the same companies; the rules drop those."""
    jobs: dict[str, Job] = {}
    for role in _words(source, "software-engineer"):
        for page in range(1, WELLFOUND_MAX_PAGES + 1):
            time.sleep(PAGE_DELAY)
            try:
                text = get_text(WELLFOUND_PAGE.format(role=role), params={"page": page} if page > 1 else None,
                                headers=BROWSER_HEADERS)
            except SourceError as e:
                if e.reason == "not_found":
                    break
                raise
            batch = wellfound_jobs(text, source)
            for job in batch:
                jobs.setdefault(job.ats_id, job)
            if not batch or all(j.ats_id in known for j in batch):
                break
    return list(jobs.values())


# ---------------------------------------------------------------- Internshala

INTERNSHALA_PAGE = "https://internshala.com/{path}/"
INTERNSHALA_MAX_PAGES = 3
_IS_CARD_RE = re.compile(r'internshipId="(\d+)"[^>]*employment_type="(\w+)"[^>]*data-href=\'([^\']+)\'(.*?)'
                         r'(?=internshipId="|<div id="pagination|$)', re.S)
_IS_FIELDS = {
    "title": re.compile(r'class="job-title-href"[^>]*>(.*?)</a>', re.S),
    "company": re.compile(r'class="company-name"[^>]*>(.*?)</p>', re.S),
    "place": re.compile(r'class="row-1-item locations">(.*?)</(?:p|div)>', re.S),
    "pay": re.compile(r'ic-16-money"></i>\s*<span[^>]*>(.*?)</span>', re.S),
    "about": re.compile(r'class="about_job">.*?<div class="text">(.*?)</div>', re.S),
    "exp": re.compile(r'ic-16-briefcase"></i>\s*<span[^>]*>(.*?)</span>', re.S),
    "posted": re.compile(r'ic-16-reschedule"></i>\s*<span[^>]*>(.*?)</span>', re.S),
}
_IS_SKILL_RE = re.compile(r"class='job_skill'>(.*?)</div>")


def internshala_cards(page: str) -> list[dict]:
    cards = []
    for job_id, kind, href, body in _IS_CARD_RE.findall(page):
        card = {"id": job_id, "kind": kind, "href": href}
        for name, rx in _IS_FIELDS.items():
            m = rx.search(body)
            card[name] = " ".join(n.html_to_text(m.group(1)).split()) if m else ""
        card["skills"] = [n.html_to_text(s) for s in _IS_SKILL_RE.findall(body)]
        if card["title"]:
            cards.append(card)
    return cards


def internshala_job(card: dict, source: Source) -> Job:
    place = card["place"].replace("(Hybrid)", " (Hybrid)")
    if "work from home" in place.lower():
        place = "Remote (India)"
    pay = card["pay"]
    salary = f"{pay} (stipend)" if card["kind"] == "internship" and pay else pay
    description = card["about"] + (f"<p>Skills: {', '.join(card['skills'])}</p>" if card["skills"] else "")
    if card.get("exp"):
        description += f"<p>Experience: {card['exp']}</p>"
    exp = (0, 0) if card["kind"] == "internship" or "no experience" in card.get("exp", "").lower() else None
    return _job(source, card["id"], card["title"], card["company"], place, description,
                "https://internshala.com" + card["href"], ago(card["posted"]), salary=salary or None,
                department="Internship" if card["kind"] == "internship" else None, exp=exp)


def fetch_internshala(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    jobs: dict[str, Job] = {}
    for path in _words(source, "internships/computer-science-internship"):
        for page in range(1, INTERNSHALA_MAX_PAGES + 1):
            time.sleep(PAGE_DELAY)
            url = INTERNSHALA_PAGE.format(path=path.strip("/") + (f"/page-{page}" if page > 1 else ""))
            try:
                text = get_text(url, headers=BROWSER_HEADERS)
            except SourceError as e:
                if e.reason == "not_found":
                    break
                raise
            batch = [internshala_job(c, source) for c in internshala_cards(text)]
            for job in batch:
                jobs.setdefault(job.ats_id, job)
            if not batch or all(j.ats_id in known for j in batch):
                break
    return list(jobs.values())


# ---------------------------------------------------------------- The Muse

THEMUSE_API = "https://www.themuse.com/api/public/jobs"
THEMUSE_MAX_PAGES = 3         # 20 jobs a page. The place filter also returns many US and remote jobs, and
                              # the list is not in date order, so a fixed number of pages is read.
THEMUSE_PLACES = "Bengaluru, India|Hyderabad, India|Pune, India|Mumbai, India|Gurgaon, India|Chennai, India|Noida, India"


def themuse_job(raw: dict, source: Source) -> Job:
    places = " | ".join(loc.get("name", "") for loc in raw.get("locations") or [])
    return _job(source, raw["id"], raw["name"], (raw.get("company") or {}).get("name") or "", places,
                raw.get("contents") or "", (raw.get("refs") or {}).get("landing_page") or "https://www.themuse.com/jobs",
                _iso(raw.get("publication_date")), department=", ".join(c.get("name", "") for c in raw.get("categories") or []),
                in_india=False)


def fetch_themuse(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    cutoff = _cutoff()
    places = [p.strip() for p in (source.board or THEMUSE_PLACES).split("|") if p.strip()]
    jobs: dict[str, Job] = {}
    for place in places:
        for page in range(THEMUSE_MAX_PAGES):
            time.sleep(PAGE_DELAY)
            data = get_json(THEMUSE_API, params={"location": place, "page": page, "descending": "true"},
                            headers=BROWSER_HEADERS)
            for raw in data.get("results") or []:
                job = themuse_job(raw, source)
                if _fresh(job, cutoff):
                    jobs.setdefault(job.ats_id, job)
            if page + 1 >= (data.get("page_count") or 0):
                break
    return list(jobs.values())


FETCHERS = {
    "linkedin": fetch_linkedin,
    "hirist": fetch_hirist,
    "iimjobs": fetch_hirist,
    "instahyre": fetch_instahyre,
    "unstop": fetch_unstop,
    "foundit": fetch_foundit,
    "shine": fetch_shine,
    "timesjobs": fetch_timesjobs,
    "ambitionbox": fetch_ambitionbox,
    "wellfound": fetch_wellfound,
    "internshala": fetch_internshala,
    "themuse": fetch_themuse,
}


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "hirist"
    found = FETCHERS[kind](Source(name=kind, type=kind, board=sys.argv[2] if len(sys.argv) > 2 else ""))
    for j in found:
        print(f"{j.company[:20]:<20}  {j.title[:50]:<50}  {j.location_raw[:30]:<30}  {j.india_eligible}  "
              f"{j.posted_at.date() if j.posted_at else '-'}")
    print(f"\n{len(found)} jobs on {kind}")
