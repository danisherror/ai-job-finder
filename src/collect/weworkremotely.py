"""We Work Remotely adapter: remote jobs from the site's public category RSS feeds.

`board` is a comma-separated list of category slugs, as in
weworkremotely.com/categories/remote-<slug>-jobs.rss, e.g. "back-end-programming,devops-sysadmin".

This is a job board (many companies; `company` is the real employer). Most of its jobs are open
"Anywhere in the World", which counts as India-eligible unless the description restricts it.
The Apply link is the job's We Work Remotely page.
"""

import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from src import normalize as n
from src.collect.http import SourceError, get_text
from src.models import Job, Source

FEED = "https://weworkremotely.com/categories/remote-{slug}-jobs.rss"
MAX_AGE_DAYS = 30
FEED_DELAY = 1.0
WORLDWIDE = "anywhere in the world"


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    jobs: dict[str, Job] = {}
    for i, slug in enumerate(s.strip() for s in source.board.split(",") if s.strip()):
        if i:
            time.sleep(FEED_DELAY)
        for item in parse_feed(get_text(FEED.format(slug=slug))):
            job = to_job(item, source)
            if job.ats_id not in jobs and (job.posted_at is None or job.posted_at >= cutoff):
                jobs[job.ats_id] = job
    return list(jobs.values())


def parse_feed(text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SourceError("bad_response", "not RSS") from e
    return [{child.tag: (child.text or "").strip() for child in item} for item in root.iter("item")]


def location_of(region: str) -> str:
    if not region or region.lower() == WORLDWIDE:
        return "Remote - Worldwide"
    return f"Remote - {region}"


def to_job(item: dict[str, str], source: Source) -> Job:
    company, _, title = item.get("title", "").partition(": ")
    if not title:  # no "Company: " prefix
        company, title = "", company
    location = location_of(item.get("region", ""))
    description = n.html_to_text(item.get("description"))
    exp_min, exp_max = n.experience_range(description)
    try:
        posted = parsedate_to_datetime(item["pubDate"])
    except (KeyError, TypeError, ValueError):
        posted = None
    link = item.get("link") or item["guid"]
    return Job(
        source=source.name,
        ats_id=item.get("guid") or link,
        title=title.strip(),
        company=company.strip() or "Unknown",
        location_raw=location,
        cities=n.indian_cities(location),
        work_mode="remote",
        india_eligible=n.india_eligibility(location, "remote", description),
        experience_min=exp_min,
        experience_max=exp_max,
        department=item.get("category") or None,
        description=description,
        apply_url=link,
        posted_at=posted,
    )


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "back-end-programming,full-stack-programming"
    jobs = fetch(Source(name="We Work Remotely", type="weworkremotely", board=board))
    for j in jobs:
        print(f"{j.company[:20]:<20}  {j.title[:50]:<50}  {j.location_raw[:25]:<25}  {j.india_eligible}")
    print(f"\n{len(jobs)} recent jobs on We Work Remotely ({board})")
