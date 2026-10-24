"""Avature adapter: reads a company's Avature career site (Siemens).

`board` is the career site's portal URL plus the search words, e.g.
https://jobs.siemens.com/en_US/externaljobs?search=India
Avature has no public JSON. The search page lists only 6 jobs per page, so a scan reads every
page of the list (Siemens: ~150 pages for ~900 jobs, a few minutes) and opens each new job's page
once for its location, date and description. At most MAX_NEW new jobs are opened per scan, newest
first; the rest wait for the next scan, so the first scan of a big site is spread over a few days.
The search words match anywhere in a job, so a few non-India jobs come through; the rules drop them
by location. robots.txt allows both pages.
"""

import html
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from src import normalize as n
from src.collect.http import SourceError, get_text
from src.models import Job, Source

MAX_JOBS = 1500     # safety cap on list entries per scan
MAX_NEW = 150       # new job pages opened per scan
DELAY = 1.0
_ROW_RE = re.compile(r'<article class="article article--result.*?</article>', re.S)
_LINK_RE = re.compile(r'<a class="link" href="([^"]*/JobDetail/(?:[^"/]*/)?(\d+))"[^>]*>\s*(.*?)\s*</a>', re.S)
_FIELD_RE = re.compile(r'<div class="article__content__view__field\b[^"]*"\s*>(.*?)</div>\s*</div>', re.S)
_LABEL_RE = re.compile(r'__field__label"\s*>(.*?)</div>', re.S)
_VALUE_RE = re.compile(r'__field__value"\s*>(.*)', re.S)
_TOTAL_RE = re.compile(r'aria-label="(\d[\d,]*) results"')


def site(board: str) -> tuple[str, str]:
    """(portal URL, search words)."""
    u = urlsplit(board if "://" in board else "https://" + board)
    if not u.netloc:
        raise SourceError("bad_config", f"Avature board needs the portal URL: {board}")
    words = (parse_qs(u.query).get("search") or [""])[0]
    return f"https://{u.netloc}{u.path.rstrip('/')}", words


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def list_page(page: str) -> list[tuple[str, str, str]]:
    """(job ID, job URL, title) for each job on a search page."""
    found = []
    for row in _ROW_RE.findall(page):
        link = _LINK_RE.search(row)
        if link:
            found.append((link.group(2), html.unescape(link.group(1)), _text(link.group(3))))
    return found


def job_fields(page: str) -> tuple[dict[str, str], str]:
    """The labelled fields on a job page ("Location(s)" -> "Gurgaon - Haryana - India | ..."),
    and the description (the field without a label)."""
    fields, description = {}, ""
    for block in _FIELD_RE.findall(page + "</div>"):
        label = _LABEL_RE.search(block)
        value = _VALUE_RE.search(block)
        raw = value.group(1) if value else ""
        if label:
            lines = [_text(line) for line in re.split(r"<br\s*/?>|</p>|</li>|\n", raw)]
            fields[_text(label.group(1))] = " | ".join(line for line in lines if line)
        elif len(raw) > len(description):
            description = raw
    return fields, n.html_to_text(description)


def _posted(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value, "%d-%b-%Y").replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


def to_job(job_id: str, url: str, title: str, page: str | None, source: Source) -> Job:
    fields, description = job_fields(page) if page else ({}, "")
    location = fields.get("Location(s)") or fields.get("Location") or ""
    mode = n.work_mode(location, fields.get("Work mode", "").split(" (")[0])
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=job_id,
        title=title,
        company=source.name,
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode=mode if page else "unknown",
        india_eligible=n.india_eligibility(location, mode, description) if page else "unclear",
        experience_min=exp_min,
        experience_max=exp_max,
        department=fields.get("Field of work") or None,
        description=description,
        apply_url=url,
        posted_at=_posted(fields.get("Posted since")),
    )


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    portal, words = site(source.board)
    cap = source.max_jobs or MAX_JOBS
    jobs: list[Job] = []
    opened = 0
    offset, total = 0, cap
    while offset < min(total, cap):
        if offset:
            time.sleep(DELAY)
        page = get_text(f"{portal}/SearchJobs/", params={"search": words, "folderSort": "postedDate",
                                                        "folderSortDirection": "DESC", "folderOffset": offset})
        m = _TOTAL_RE.search(page)
        total = int(m.group(1).replace(",", "")) if m else 0
        rows = list_page(page)
        if not rows:
            break
        for job_id, url, title in rows:
            detail = None
            if job_id not in known:
                if opened >= MAX_NEW:
                    continue    # left for the next scan; a job saved without its page would stay blank
                time.sleep(DELAY)
                detail = get_text(url)
                opened += 1
            jobs.append(to_job(job_id, url, title, detail, source))
        offset += len(rows)
    return jobs


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "https://jobs.siemens.com/en_US/externaljobs?search=India"
    found = fetch(Source(name="test", type="avature", board=board, max_jobs=12))   # first 2 pages
    for j in found:
        print(f"{j.title[:50]:<50}  {j.location_raw[:40]:<40}  {j.work_mode:<7} {j.india_eligible:<7}  "
              f"{j.posted_at:%Y-%m-%d}  {len(j.description)}")
    print(f"\n{len(found)} jobs on {board}")
