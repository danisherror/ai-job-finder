"""Oracle Cloud HCM adapter: reads a company's Oracle Recruiting career site API (India jobs only).

`board` is the career site URL, e.g.
https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001
Some companies serve it from their own domain (Dell: enterpriseplatform.dell.com).
This is the public JSON the career site itself loads. The list has no full descriptions,
so each new job costs one extra call; jobs we already store are returned without one.
"""

import re
import sys
import time
from urllib.parse import quote, urlsplit

from src import normalize as n
from src.collect.http import SourceError, get_json
from src.models import Job, Source

PAGE = 25           # Oracle's page size
MAX_JOBS = 1000     # safety cap per company
DETAIL_DELAY = 0.2
_SITE_RE = re.compile(r"/hcmUI/CandidateExperience/(?:[a-z]{2}/)?sites/([^/?#]+)")


def site(board: str) -> tuple[str, str]:
    """(host, site number) from a career site URL."""
    u = urlsplit(board if "://" in board else "https://" + board)
    m = _SITE_RE.search(u.path)
    if not u.netloc or not m:
        raise SourceError("bad_config", f"not an Oracle career site URL: {board}")
    return u.netloc, m.group(1)


def _search(host: str, site_no: str, **finder) -> dict:
    parts = ",".join([f"siteNumber={site_no}"] + [f"{k}={v}" for k, v in finder.items()])
    url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;{parts}")
    items = get_json(url).get("items") or [{}]
    return items[0]


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    host, site_no = site(source.board)
    jobs: list[Job] = []
    offset, total = 0, MAX_JOBS
    while offset < min(total, MAX_JOBS):
        # location=India matches the country's own location ID; the location facet only lists the
        # top 10 places, so India is missing from it on sites like Dell's.
        data = _search(host, site_no, limit=PAGE, offset=offset, location="India", sortBy="POSTING_DATES_DESC")
        total = data.get("TotalJobsCount") or 0
        items = data.get("requisitionList") or []
        if not items:
            break
        for raw in items:
            detail = None
            if raw["Id"] not in known:
                time.sleep(DETAIL_DELAY)
                detail = _detail(host, site_no, raw["Id"])
            jobs.append(to_job(raw, detail, source))
        offset += len(items)
    return jobs


def _detail(host: str, site_no: str, job_id: str) -> dict | None:
    finder = quote(f'ById;Id="{job_id}",siteNumber={site_no}', safe=";=,")
    url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder={finder}"
    items = get_json(url).get("items") or []
    return items[0] if items else None


def to_job(raw: dict, detail: dict | None, source: Source) -> Job:
    host, site_no = site(source.board)
    d = detail or {}
    locs = [raw.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in raw.get("secondaryLocations") or []]
    location = " | ".join(dict.fromkeys(l for l in locs if l))
    sections = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
    description = "\n\n".join(n.html_to_text(d[k]) for k in sections if d.get(k))
    mode = n.work_mode(location, raw.get("WorkplaceTypeCode") or raw.get("WorkplaceType"))
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=str(raw["Id"]),
        title=raw["Title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description, raw.get("PrimaryLocationCountry")),
        experience_min=exp_min,
        experience_max=exp_max,
        department=raw.get("JobFamily") or raw.get("JobFunction"),
        description=description,
        apply_url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site_no}/job/{raw['Id']}",
        posted_at=d.get("ExternalPostedStartDate") or raw.get("PostedDate"),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"
    jobs = fetch(Source(name="test", type="oracle", board=board))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.india_eligible}  {len(j.description)}")
    print(f"\n{len(jobs)} India jobs on {board}")
