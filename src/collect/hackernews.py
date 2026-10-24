"""Hacker News "Who is hiring?" adapter: the newest monthly thread, read through HN's public Algolia API.

Each top-level comment is one company's post. Posts are free text, but most start with a header line
like "Company | Role | Location | REMOTE | Full-time". Company, title and location come from that line;
the matcher reads the whole post. Posts that name neither India nor remote work count as not open to
India, so only a few a month (about 10) reach the matcher.

This is a job board (many companies). Two calls per scan; `board` is unused. The Apply link is the
post on Hacker News, which says how to apply.
"""

import re
import sys
from datetime import datetime, timedelta, timezone

from src import normalize as n
from src.collect.http import SourceError, get_json
from src.models import Job, Source

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
POST = "https://news.ycombinator.com/item?id={id}"
THREAD_TITLE = "Ask HN: Who is hiring?"
MAX_AGE_DAYS = 30

_SPLIT_RE = re.compile(r"\s+[|•]\s+|\s*\|\s*|\s+[—–]\s+")
_PAREN_RE = re.compile(r"\(([^)]*)\)")
_URL_RE = re.compile(r"https?://\S+|\b[\w-]+\.(?:com|io|ai|co|dev|org|net|app|in)\b", re.I)
_PLACE_RE = re.compile(r"\b(remote|on-?site|hybrid|in-office|india|global|worldwide|anywhere|apac|asia)\b", re.I)
_ROLE_RE = re.compile(
    r"\b(engineers?|developers?|scientists?|designers?|managers?|leads?|architects?|analysts?|sre|devops|"
    r"researchers?|interns?|head of|directors?|founding|roles|positions|swe|cto|ml|programmers?)\b",
    re.I,
)


def latest_thread_id() -> str:
    data = get_json(SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 10})
    for hit in data.get("hits") or []:
        if (hit.get("title") or "").startswith(THREAD_TITLE):
            return hit["objectID"]
    raise SourceError("bad_response", "no Who is hiring thread found")


def parse_header(header: str) -> tuple[str, str, str]:
    """(company, title, location) from a post's first line."""
    parts = [p.strip() for p in _SPLIT_RE.split(header) if p.strip()]
    if not parts:
        return "Unknown", "Multiple roles", ""
    first = parts[0]
    company = _URL_RE.sub("", _PAREN_RE.sub("", first)).strip(" ,-") or _URL_RE.sub("", first).strip() or first
    rest = parts[1:] + _PAREN_RE.findall(first)
    places = [p for p in rest if _PLACE_RE.search(p) or n.mentions_india(p)]
    roles = [p for p in parts[1:] if _ROLE_RE.search(p) and p not in places]
    title = roles[0] if roles else "Multiple roles"
    return company[:80], title[:120], " | ".join(places)


def to_job(comment: dict, source: Source) -> Job:
    html = comment.get("text") or ""
    first, _, _ = html.partition("<p>")
    header = n.html_to_text(first).splitlines()[0] if n.html_to_text(first) else ""
    company, title, location = parse_header(header)
    description = n.html_to_text(html)
    mode = n.work_mode(location)
    exp_min, exp_max = n.experience_range(description)
    return Job(
        source=source.name,
        ats_id=str(comment["id"]),
        title=title,
        company=company,
        location_raw=location or header[:200],
        cities=n.indian_cities(location),
        work_mode=mode,
        india_eligible=n.india_eligibility(location, mode, description),
        experience_min=exp_min,
        experience_max=exp_max,
        description=description,
        apply_url=POST.format(id=comment["id"]),
        posted_at=datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00")),
    )


def fetch(source: Source, known: frozenset[str] = frozenset()) -> list[Job]:
    thread = get_json(ITEM.format(id=latest_thread_id()))
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    jobs = [to_job(c, source) for c in thread.get("children") or [] if c.get("text")]
    return [j for j in jobs if j.posted_at >= cutoff]


if __name__ == "__main__":
    jobs = fetch(Source(name="Hacker News", type="hackernews", board=""))
    for j in jobs:
        if j.india_eligible != "no" or "-v" in sys.argv:
            print(f"{j.company[:20]:<20}  {j.title[:40]:<40}  {j.location_raw[:40]:<40}  {j.india_eligible}")
    print(f"\n{len(jobs)} posts, {sum(j.india_eligible != 'no' for j in jobs)} possibly open to India")
