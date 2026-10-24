"""Eightfold adapter: reads a company's Eightfold career site search API (India jobs only).

`board` is the career site URL with its domain, e.g.
https://apply.careers.microsoft.com/careers?domain=microsoft.com
https://qualcomm.eightfold.ai/careers?domain=qualcomm.com
This is the public JSON the career site itself loads. It returns 10 jobs per page and has
no descriptions, so each new job costs one extra call. These sites answer HTTP 429 (and
Eightfold's shared servers sometimes 403) to fast requests, so calls are spaced out.
Some sites (Netflix) still run Eightfold's older API at /api/apply/v2; those are listed in
LEGACY_HOSTS and read the same way through it.
"""

import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from src import normalize as n
from src.collect.http import SourceError, get_json
from src.models import Job, Source

MAX_JOBS = 1000     # safety cap per company
DELAY = 1.5         # between calls; all *.eightfold.ai sites share servers and answered 429/403
                    # to back-to-back scans 0.3s apart
SLOW_HOSTS = {"apply.careers.microsoft.com": 2.0}   # answered HTTP 429 after ~300 calls 1s apart
LEGACY_HOSTS = {"explore.jobs.netflix.net"}          # the newer API answers "PCSX is not enabled"
_DAYS_RE = re.compile(r"(\d)\s*days?\s*/\s*week", re.I)


def site(board: str) -> tuple[str, str]:
    """(base URL, company domain) from a career site URL."""
    u = urlsplit(board if "://" in board else "https://" + board)
    domain = (parse_qs(u.query).get("domain") or [""])[0]
    if not u.netloc or not domain:
        raise SourceError("bad_config", f"Eightfold board needs ?domain=<company domain>: {board}")
    return f"https://{u.netloc}", domain


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    base, domain = site(source.board)
    if urlsplit(base).netloc in LEGACY_HOSTS:
        return fetch_legacy(source, known)
    delay = SLOW_HOSTS.get(urlsplit(base).netloc, DELAY)
    jobs: list[Job] = []
    cap = source.max_jobs or MAX_JOBS
    start, total = 0, cap
    while start < min(total, cap):
        if start:
            time.sleep(delay)
        data = get_json(f"{base}/api/pcsx/search", params={
            "domain": domain, "query": "", "location": "India", "start": start, "sort_by": "timestamp"})
        data = data.get("data") or {}
        total = data.get("count") or 0
        items = data.get("positions") or []
        if not items:
            break
        for raw in items[:cap - start]:
            detail = None
            if str(raw["id"]) not in known:
                time.sleep(delay)
                detail = get_json(f"{base}/api/pcsx/position_details",
                                  params={"position_id": raw["id"], "domain": domain, "hl": "en"}).get("data")
            jobs.append(to_job(raw, detail, source))
        start += len(items)
    return jobs


def fetch_legacy(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    """The older /api/apply/v2 API: same paging, different field names."""
    base, domain = site(source.board)
    jobs: list[Job] = []
    cap = source.max_jobs or MAX_JOBS
    start, total = 0, cap
    while start < min(total, cap):
        if start:
            time.sleep(DELAY)
        data = get_json(f"{base}/api/apply/v2/jobs",
                        params={"domain": domain, "location": "India", "start": start, "num": 10})
        total = data.get("count") or 0
        items = data.get("positions") or []
        if not items:
            break
        for raw in items[:cap - start]:
            detail = None
            if str(raw["id"]) not in known:
                time.sleep(DELAY)
                detail = get_json(f"{base}/api/apply/v2/jobs/{raw['id']}", params={"domain": domain})
            jobs.append(legacy_job(raw, detail, source))
        start += len(items)
    return jobs


def legacy_job(raw: dict, detail: dict | None, source: Source) -> Job:
    base, _ = site(source.board)
    d = detail or {}
    location = " | ".join(dict.fromkeys(raw.get("locations") or [raw.get("location") or ""]))
    description = n.html_to_text(d.get("job_description"))
    mode = n.work_mode(location, raw.get("work_location_option"))
    exp_min, exp_max = n.experience_range(description)
    ts = raw.get("t_create") or raw.get("t_update")
    return Job(
        source=source.name,
        ats_id=str(raw["id"]),
        title=(raw.get("posting_name") or raw["name"]).strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=raw.get("department"),
        description=description,
        apply_url=raw.get("canonicalPositionUrl") or f"{base}/careers/job/{raw['id']}",
        posted_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
    )


def _mode(raw: dict, d: dict) -> str | None:
    """Eightfold's work option, but "3 days / week in-office" counts as hybrid."""
    worksite = " ".join(d.get("efcustomTextWorkSite") or [])
    days = _DAYS_RE.search(worksite)
    if days and int(days.group(1)) < 5:
        return "hybrid"
    return d.get("workLocationOption") or raw.get("workLocationOption")


def to_job(raw: dict, detail: dict | None, source: Source) -> Job:
    base, _ = site(source.board)
    d = detail or {}
    location = " | ".join(dict.fromkeys(raw.get("locations") or [d.get("location") or ""]))
    description = n.html_to_text(d.get("jobDescription"))
    mode = n.work_mode(location, _mode(raw, d))
    exp_min, exp_max = n.experience_range(description)
    ts = raw.get("postedTs") or raw.get("creationTs")
    return Job(
        source=source.name,
        ats_id=str(raw["id"]),
        title=raw["name"].strip(),
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=raw.get("department"),
        description=description,
        apply_url=d.get("publicUrl") or base + raw["positionUrl"],
        posted_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "https://apply.careers.microsoft.com/careers?domain=microsoft.com"
    jobs = fetch(Source(name="test", type="eightfold", board=board))
    for j in jobs:
        print(f"{j.title[:55]:<55}  {j.location_raw[:35]:<35}  {j.work_mode:<7}  {len(j.description)}")
    print(f"\n{len(jobs)} India jobs on {board}")
