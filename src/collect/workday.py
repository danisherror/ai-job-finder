"""Workday adapter: reads a company's career site search API (India jobs only).

`board` is the career site URL, e.g. https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
Workday has no official public feed; this is the same JSON the career site itself loads.
Like SmartRecruiters, descriptions need one call per job, so only new jobs are fetched in full.
"""

import re
import sys
import time
from urllib.parse import urlsplit

from src import normalize as n
from src.collect.http import SourceError, get_json, post_json
from src.models import Job, Source

PAGE = 20          # Workday's maximum page size
MAX_JOBS = 1000    # safety cap per company
DETAIL_DELAY = 0.2
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_INDIA_RE = re.compile(r"\bindia\b", re.I)   # "India, Pune" and "Bengaluru, Karnataka, India", not "Indiana"
_IND_CODE_RE = re.compile(r"^IND\b")          # Broadcom's "IND-Bangalore Electronic City"


def site_urls(board: str) -> tuple[str, str]:
    """(api base, public base) from a career site URL."""
    u = urlsplit(board if "://" in board else "https://" + board)
    segs = [s for s in u.path.split("/") if s and not _LOCALE_RE.match(s)]
    if not segs or "myworkdayjobs.com" not in u.netloc:
        raise SourceError("bad_config", f"not a Workday career site URL: {board}")
    tenant, site = u.netloc.split(".")[0], segs[0]
    return f"https://{u.netloc}/wday/cxs/{tenant}/{site}", f"https://{u.netloc}/{site}"


def india_facet(facets: list[dict]) -> tuple[str, list[str]] | None:
    """The search filter for India: a country-level "India" value, else every city value naming India,
    else every value naming an Indian city ("Bengaluru", "Chennai, DLF IT Park"), as GE and Barclays list them."""
    found = _india_facet(facets)
    return found[1] if found else None


def _india_facet(facets: list[dict]) -> tuple[int, tuple[str, list[str]]] | None:
    """(rank, facet): rank 0 is a single country value, 1 values naming India, 2 values naming only a city."""
    best: tuple[int, tuple[str, list[str]]] | None = None
    for facet in facets:
        param, values = facet.get("facetParameter"), facet.get("values") or []
        nested = [v for v in values if "facetParameter" in v]
        if nested:
            found = _india_facet(nested)
            if found and found[0] < 2 and len(found[1][1]) == 1:
                return 0, found[1]
            if found and (not best or found[0] < best[0]):
                best = found
            continue
        exact = [v["id"] for v in values if (v.get("descriptor") or "").strip().lower() == "india"]
        if exact:
            return 0, (param, exact)
        some = [v["id"] for v in values
                if _INDIA_RE.search(v.get("descriptor") or "") or _IND_CODE_RE.match(v.get("descriptor") or "")]
        if some and (not best or best[0] > 1):
            best = 1, (param, some)
        city = [v["id"] for v in values if n.indian_cities(v.get("descriptor") or "")]
        if city and not best:
            best = 2, (param, city)
    return best


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    api, public = site_urls(source.board)
    first = post_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    facet = india_facet(first.get("facets") or [])
    if not facet:
        return []  # no India locations on this site
    applied = {facet[0]: facet[1]}
    jobs: list[Job] = []
    cap = source.max_jobs or MAX_JOBS
    total, offset = None, 0
    while offset < min(total if total is not None else cap, cap):
        data = post_json(f"{api}/jobs", {"appliedFacets": applied, "limit": PAGE, "offset": offset, "searchText": ""})
        if total is None:
            total = data.get("total") or 0   # later pages report 0
        items = data.get("jobPostings") or []
        if not items:
            break
        for raw in items[:cap - offset]:
            if not raw.get("externalPath") or not raw.get("title"):
                continue  # a stub with only bulletFields (Accenture lists some); there is no job page
            detail = None
            if job_id(raw) not in known:
                time.sleep(DETAIL_DELAY)
                detail = get_json(api + raw["externalPath"]).get("jobPostingInfo")
            jobs.append(to_job(raw, detail, source, public))
        offset += len(items)
    return jobs


def job_id(raw: dict) -> str:
    return raw["externalPath"].rsplit("/", 1)[-1]


def to_job(raw: dict, detail: dict | None, source: Source, public: str) -> Job:
    d = detail or {}
    locs = [d.get("location") or raw.get("locationsText") or ""] + list(d.get("additionalLocations") or [])
    location = " | ".join(dict.fromkeys(l for l in locs if l))
    country = ((d.get("jobRequisitionLocation") or {}).get("country") or {}).get("alpha2Code")
    description = n.html_to_text(d.get("jobDescription"))
    mode = n.work_mode(location, d.get("remoteType"))
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=job_id(raw),
        title=raw["title"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description, country),
        experience_min=exp_min,
        experience_max=exp_max,
        description=description,
        apply_url=d.get("externalUrl") or public + raw["externalPath"],
        posted_at=d.get("startDate"),
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    jobs = fetch(Source(name="test", type="workday", board=board))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.india_eligible}  {len(j.description)}")
    print(f"\n{len(jobs)} India jobs on {board}")
