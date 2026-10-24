"""RippleHire adapter: reads a company's RippleHire career site search (LTIMindtree, Mphasis, Tata Technologies).

`board` is the career site link the company publishes, e.g.
https://ltimindtree.ripplehire.com/candidate/?token=xviyQvbnyYZdGtozXoNm#list/geo=India
The token names the company's career site. Filters after "#list/" (geo=India) are passed to the search;
sites without a country filter (Tata Technologies, Mphasis) return every job and the rules sort them out.
The list has no descriptions, so each new job costs one extra call, like Eightfold.
"""

import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from src import normalize as n
from src.collect.http import SourceError, get_json, post_form
from src.models import Job, Source

PAGE_SIZE = 100
MAX_JOBS = 1000     # safety cap per company
DELAY = 1.0
JSON = {"Accept": "application/json"}   # without it the site answers in XML


def site(board: str) -> tuple[str, str, dict[str, str]]:
    """(base URL, token, search filters) from a career site link."""
    u = urlsplit(board if "://" in board else "https://" + board)
    token = (parse_qs(u.query).get("token") or [""])[0]
    if not u.netloc or not token:
        raise SourceError("bad_config", f"RippleHire board needs ?token=<site token>: {board}")
    filters = {}
    fragment = u.fragment.removeprefix("list").strip("/")
    for key, values in parse_qs(fragment).items():
        filters[key] = values[0]
    return f"https://{u.netloc}", token, filters


def job_page(base: str, token: str, job_id: str) -> str:
    return f"{base}/candidate/?token={token}&source=CAREERSITE#detail/job/{job_id}"


def _posted(d: dict) -> datetime | None:
    published = (d.get("publishDetails") or {}).get("CAREER_SITE")
    try:
        if published:
            return datetime.fromisoformat(published.replace("Z", "+00:00"))
        if d.get("jobPostingDate"):
            return datetime.strptime(d["jobPostingDate"], "%d-%b-%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def _location(raw: dict, searched_country: str = "") -> str:
    """"Bengaluru" with country "India" or "IND" -> "Bengaluru, India". A job found by a search limited
    to one country (geo=India) is in that country even when the job leaves the country out."""
    city = (raw.get("locations") or "").strip()
    country = (raw.get("jobLocation") or "").strip() or searched_country
    country = "India" if country.upper() in ("IND", "IN") else country
    if country and country.lower() not in city.lower() and country != city:
        return f"{city}, {country}".strip(", ")
    return city or country


def to_job(raw: dict, detail: dict | None, source: Source) -> Job:
    base, token, filters = site(source.board)
    d = detail or {}
    location = _location(raw, filters.get("geo", "") if "," not in filters.get("geo", "") else "")
    parts = [d.get("jobDesc"), d.get("jobSkills")]
    description = "\n\n".join(n.html_to_text(p) for p in parts if p)
    mode = n.work_mode(f"{location} {raw.get('jobTitle', '')}")
    exp_min, exp_max = raw.get("jobMinExp"), raw.get("jobMaxExp")
    if not isinstance(exp_min, int) or not isinstance(exp_max, int) or exp_max > 50:
        exp_min, exp_max = n.experience_range(f"{raw.get('jobReqExp') or ''} years\n{description}")
    return Job(
        source=source.name,
        ats_id=str(raw["jobSeq"]),
        title=raw["jobTitle"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=d.get("jobTypeCustom3") or None,
        description=description,
        apply_url=job_page(base, token, raw["jobSeq"]),
        posted_at=_posted(d),
    )


def search(base: str, params: dict) -> dict:
    data = post_form(f"{base}/candidate/candidatejobsearch",
                     {"careerSiteUrlParams": json.dumps(params), "lang": "en"}, headers=JSON)
    if not isinstance(data, dict):
        raise SourceError("bad_response", "expected a job page")
    return data


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    base, token, filters = site(source.board)
    cap = source.max_jobs or MAX_JOBS
    jobs: list[Job] = []
    page, total = 0, cap
    while len(jobs) < min(total, cap):
        if page:
            time.sleep(DELAY)
        data = search(base, {"page": page, "search": "*:*", "token": token, "source": "CAREERSITE",
                             "pagesize": PAGE_SIZE, **filters})
        total = int(data.get("totalJobCount") or 0)
        items = data.get("jobVoList") or []
        if not items:
            break
        for raw in items[:cap - len(jobs)]:
            detail = None
            if str(raw["jobSeq"]) not in known:
                time.sleep(DELAY)
                detail = (get_json(f"{base}/candidate/candidatejobdetail", headers=JSON, params={
                    "token": token, "jobSeq": raw["jobSeq"], "source": "CAREERSITE", "lang": "en"}) or {}).get("jobVO")
            jobs.append(to_job(raw, detail, source))
        page += 1
    return jobs


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else \
        "https://ltimindtree.ripplehire.com/candidate/?token=xviyQvbnyYZdGtozXoNm#list/geo=India"
    found = fetch(Source(name="test", type="ripplehire", board=board, max_jobs=20))
    for j in found:
        print(f"{j.title[:50]:<50}  {j.location_raw[:30]:<30}  {j.india_eligible:<7}  {len(j.description)}")
    print(f"\n{len(found)} jobs on {board}")
