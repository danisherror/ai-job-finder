"""The shared lists every bot subscriber gets: software internships and jobs for 2027 graduates in India.

Anyone who presses Start in the bot is subscribed. No profile and no AI: everyone gets the same lists, built
from the same rules as private/exports/india_internships.csv and graduates_2027.csv. An internship goes only
in the internship list, never in both.
"""

import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone

from src import health, store, telegram
from src.digest import (GRAD_YEAR, _date, e, grad_reason_order, graduate_reason, is_india_internship, place,
                        safe_apply_url, tz, usual_hosts)

MAX_AGE_DAYS = 14     # leave out postings older than this, even when a newly added source finds them today
SEARCH_DAYS = 30      # /search, /city and /watch look this far back
MESSAGE_LIMIT = 3800  # Telegram allows 4096 characters per message
SEND_PAUSE = 0.5      # seconds between messages; stays under Telegram's rate limits

SHORT_REASON = {f"{GRAD_YEAR} batch": f"🎯 {GRAD_YEAR} batch", "campus / graduate / fresher role": "campus / fresher",
                "fresher role (0 years)": "0 years"}

WELCOME = ("👋 <b>Welcome!</b> You're subscribed.\n\n"
           "Every morning by 7 AM IST you'll get the new <b>software internships</b> and "
           f"<b>jobs for {GRAD_YEAR} graduates</b> open to people in India, from company career sites, "
           "job boards and Indian job portals.\n\n"
           "Below is everything from the last 2 weeks. /help shows what else I can do.")
HELP = (f"I send new software internships and jobs for {GRAD_YEAR} graduates in India every morning by 7 AM IST.\n\n"
        "<b>Find jobs</b>\n"
        "/latest · the last 2 weeks' jobs as a file\n"
        "/search react · jobs matching words (title, company or place)\n"
        "/city pune · jobs in a city (or /city remote)\n"
        "/stats · numbers on what's out there\n\n"
        "<b>Your daily list</b>\n"
        "/filters · only some cities, or only internships / jobs\n"
        "/watch google · point out a company's new jobs\n"
        "/pause 7 · no lists for 7 days · /resume\n\n"
        "<b>Track</b>\n"
        "Tap the /a_… under a job after you apply · /applied lists them\n\n"
        "/feedback your message · tell the person who runs this bot\n"
        "/stop unsubscribes · /start subscribes again · /id shows your chat ID")
COMMANDS = [{"command": "latest", "description": "The last 2 weeks' jobs as a file"},
            {"command": "search", "description": "Find jobs by word, e.g. /search react"},
            {"command": "city", "description": "Jobs in a city, e.g. /city pune"},
            {"command": "filters", "description": "Choose cities and job types for your daily list"},
            {"command": "watch", "description": "Follow a company's new jobs"},
            {"command": "applied", "description": "Jobs you marked as applied"},
            {"command": "stats", "description": "Numbers on current jobs"},
            {"command": "pause", "description": "Pause the daily list for some days"},
            {"command": "resume", "description": "Restart the daily list"},
            {"command": "feedback", "description": "Send a message to the bot's owner"},
            {"command": "help", "description": "What this bot does"},
            {"command": "stop", "description": "Stop the daily lists"},
            {"command": "start", "description": "Subscribe to the daily lists"},
            {"command": "id", "description": "Show your chat ID"}]
OWNER_COMMANDS = COMMANDS + [{"command": "active", "description": "Who is subscribed (owner only)"}]


def set_commands() -> None:
    """Show the command list in Telegram's / menu: the public list for everyone, plus /active for the owner."""
    telegram.api("setMyCommands", commands=COMMANDS)
    if owner := health.operator_chat():
        telegram.api("setMyCommands", commands=OWNER_COMMANDS, scope={"type": "chat", "chat_id": owner})


# ---------------------------------------------------------------- picking

def _when(r) -> datetime | None:
    value = r["posted_at"] or r["first_seen"]
    try:
        d = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pick_lists(conn, since: str | None = None, now: datetime | None = None, words: list[str] = (),
               max_age_days: int = MAX_AGE_DAYS, include_backfill: bool = False) -> tuple[list, list]:
    """(internships, [(job, reason)] for graduates), newest first; graduates grouped by reason.
    since: only jobs first seen after it. words: each must appear in the title, company or place (for /search).
    include_backfill: also jobs that were already up before a source was first scanned."""
    now = now or datetime.now(timezone.utc)
    oldest = now - timedelta(days=max_age_days)
    sql = """SELECT j.*, s.type AS source_type, s.board FROM jobs j JOIN sources s ON s.name = j.source
             WHERE j.lifecycle = 'open' AND j.india_eligible = 'yes'"""
    params: tuple = ()
    if not include_backfill:
        sql += " AND j.backfill = 0"
    if since:
        sql += " AND j.first_seen > ?"
        params += (since,)
    for word in words:
        like = word.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += (" AND LOWER(j.title || ' ' || j.company || ' ' || COALESCE(j.location_raw, '') || ' '"
                " || COALESCE(j.cities, '')) LIKE ? ESCAPE '\\'")
        params += (f"%{like}%",)
    rows = conn.execute(sql + " ORDER BY COALESCE(j.posted_at, j.first_seen) DESC", params).fetchall()
    internships, graduates, seen = [], [], {}
    for r in rows:
        when = _when(r)
        if when and when < oldest:
            continue
        # The same posting in several cities is listed once (the newest), with a count of the other places.
        key = (r["title"].strip().lower(), r["company"].strip().lower())
        if key in seen:
            seen[key] += 1
            continue
        seen[key] = 0
        if is_india_internship(r):
            internships.append(r)
        elif reason := graduate_reason(r):
            graduates.append((r, reason))
    order = grad_reason_order()
    graduates.sort(key=lambda rr: order.index(rr[1]))  # stable: newest first inside each group
    more = {key: n for key, n in seen.items() if n}
    if more:  # sqlite3.Row is read-only, so extra places go in a plain dict copy
        def with_more(r):
            n = more.get((r["title"].strip().lower(), r["company"].strip().lower()))
            return {**dict(r), "more_places": n} if n else r
        internships = [with_more(r) for r in internships]
        graduates = [(with_more(r), why) for r, why in graduates]
    return internships, graduates


# ---------------------------------------------------------------- filters

JOB_TYPES = {"internships": "internships only", "jobs": f"{GRAD_YEAR}-graduate jobs only"}


def in_city(r, city: str) -> bool:
    if city == "remote":
        return r["work_mode"] == "remote"
    return city in json.loads(r["cities"] or "[]") or city.lower() in (r["location_raw"] or "").lower()


def apply_filters(internships: list, graduates: list, filters: dict) -> tuple[list, list]:
    """Keep only the jobs a subscriber asked for: some cities ("remote" counts as one), and/or one list."""
    cities = filters.get("cities") or []

    def wanted(r):
        return not cities or any(in_city(r, c) for c in cities)
    kind = filters.get("type")
    return ([r for r in internships if kind != "jobs" and wanted(r)],
            [(r, why) for r, why in graduates if kind != "internships" and wanted(r)])


def describe_filters(filters: dict) -> str:
    parts = [JOB_TYPES[t]] if (t := filters.get("type")) in JOB_TYPES else []
    if cities := filters.get("cities"):
        parts.append(", ".join(cities))
    return " · ".join(parts) or "none, you get every job"


# ---------------------------------------------------------------- formatting

def job_line(r, url: str, tag: str | None = None, job_id: int | None = None) -> str:
    """One job: its title as the apply link, then company, place, and the /a_<id> to tap after applying."""
    where = e(place(r))
    if extra := (r["more_places"] if "more_places" in r.keys() else 0):
        where += f" (+{extra} more place{'s' if extra > 1 else ''})"
    details = " · ".join(x for x in (e(r["company"]), where, tag, f"✅ /a_{job_id}" if job_id else "") if x)
    return f'• <a href="{e(url)}">{e(r["title"])}</a>\n   {details}'


def section(title: str, lines: list[str], total: int, label: str = "new") -> list[str]:
    """One list as one or more messages under MESSAGE_LIMIT, with a count."""
    head = f"<b>{title}</b> · {total} {label}" if total else f"<b>{title}</b>\nNothing {label}."
    messages, current = [], head
    for line in lines:
        if len(current) + len(line) + 2 > MESSAGE_LIMIT:
            messages.append(current)
            current = line
        else:
            current += "\n\n" + line
    messages.append(current)
    return messages


def jobs_csv(internships: list, graduates: list, url, ids: dict[str, int] | None = None) -> bytes:
    """Both lists in one spreadsheet, internships first. The id column is the number for /a_<id>."""
    ids = ids or {}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["list", "posted", "company", "title", "location", "why", "apply_url", "id"])
    rows = [(r, "internship", "") for r in internships] + [(r, f"{GRAD_YEAR} graduates", why) for r, why in graduates]
    for r, kind, why in rows:
        where = place(r)
        if extra := (r["more_places"] if "more_places" in r.keys() else 0):
            where += f" (+{extra} more)"
        w.writerow([kind, _date(r["posted_at"] or r["first_seen"]), r["company"], r["title"], where, why, url(r),
                    ids.get(r["job_key"], "")])
    return out.getvalue().encode("utf-8-sig")  # -sig: Excel then reads ₹, é, etc. correctly


def summary(internships: list, graduates: list, label: str) -> str:
    """Counts only: the caption of a CSV sent instead of the job list."""
    if not internships and not graduates:
        return f"Nothing {label}."
    return (f"<b>💼 Software internships</b> · {len(internships)} {label}\n"
            f"<b>🎓 Jobs for {GRAD_YEAR} graduates</b> · {len(graduates)} {label}")


def linker(conn, internships: list, graduates: list):
    """(url(job), {job_key: id}): each job's safe apply link, and its number for /a_<id>."""
    hosts = usual_hosts(conn)
    ids = store.share_ids(conn, [r["job_key"] for r in internships] + [r["job_key"] for r, _ in graduates])

    def url(r):
        return safe_apply_url(r["apply_url"], r["source_type"], r["board"], r["ats_id"] or "",
                              hosts.get(r["source"], set()))
    return url, ids


def build_messages(conn, internships: list, graduates: list, label: str = "new today",
                   summary_only: bool = False, link=None) -> tuple[list[str], bytes | None]:
    """The chat messages listing every job (or, with summary_only, just the counts),
    plus a CSV of them all (None when there are no jobs). link: a linker() result to reuse."""
    url, ids = link or linker(conn, internships, graduates)
    table = jobs_csv(internships, graduates, url, ids) if internships or graduates else None
    if summary_only:
        return [summary(internships, graduates, label)], table
    intern_lines = [job_line(r, url(r), job_id=ids.get(r["job_key"])) for r in internships]
    grad_lines = [job_line(r, url(r), SHORT_REASON.get(reason), ids.get(r["job_key"])) for r, reason in graduates]
    messages = (section("💼 Software internships", intern_lines, len(internships), label)
                + section(f"🎓 Jobs for {GRAD_YEAR} graduates", grad_lines, len(graduates), label))
    return messages, table


def watch_messages(internships: list, graduates: list, companies: list[str], link) -> list[str]:
    """The jobs (from today's new ones) at companies this person watches, or [] if none."""
    url, ids = link
    rows = [(r, None) for r in internships] + [(r, SHORT_REASON.get(why)) for r, why in graduates]
    hits = [(r, tag) for r, tag in rows if any(c in r["company"].lower() for c in companies)]
    if not hits:
        return []
    lines = [job_line(r, url(r), tag, ids.get(r["job_key"])) for r, tag in hits]
    return section("🔔 Companies you watch", lines, len(hits), "new")


def subscribers_messages(conn) -> list[str]:
    """Who uses the bot: every active subscriber, plus how many have stopped. For the owner only."""
    rows = store.all_subscribers(conn)
    active = [r for r in rows if r["active"]]
    lines = [f"• {e(r['name'] or 'No name')}" + (f" (@{e(r['username'])})" if r["username"] else "")
             + f"\n   joined {_date(r['joined_at'])} · <code>{r['chat_id']}</code>" for r in active]
    messages = section("👥 Subscribers", lines, len(active), "active")
    if stopped := len(rows) - len(active):
        messages[-1] += f"\n\n<i>{stopped} stopped or blocked the bot</i>"
    return messages


# ---------------------------------------------------------------- sending

GONE = ("blocked by the user", "user is deactivated", "chat not found", "bot was kicked")


def csv_name(label: str = "jobs") -> str:
    return f"{label}_{datetime.now(tz()).strftime('%Y-%m-%d')}.csv"


def send_to(conn, chat_id: int, messages: list[str], document: tuple[str, bytes, str] | None = None) -> bool:
    """Send messages, then an optional (filename, content, caption) file, to one chat.
    Unsubscribes chats that blocked the bot. Returns False if anything failed."""
    for item in messages + ([document] if document else []):
        try:
            if isinstance(item, str):
                telegram.send_message(chat_id, item, quiet=True)
            else:
                telegram.send_document(chat_id, item[0], item[1], caption=item[2], quiet=True)
        except telegram.TelegramError as err:
            if any(g in str(err) for g in GONE):
                store.unsubscribe(conn, chat_id)
            return False
        time.sleep(SEND_PAUSE)
    return True


def send_daily(conn, dry_run: bool = False, once_a_day: bool = True) -> dict:
    """Send the jobs first seen since the last list to every subscriber, narrowed by their /filters,
    with their watched companies' new jobs on top. Paused subscribers are skipped.

    once_a_day (the scheduled run): skip if a list already went out today, so a restarted run doesn't send twice.
    Without it (the `share` command) it sends whenever run; each job still goes out only once.
    """
    today = datetime.now(tz()).date().isoformat()
    if once_a_day and not dry_run and store.get_state(conn, "broadcast_day") == today:
        return {"skipped": "already sent today"}
    since = store.get_state(conn, "broadcast_since") or (
        datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    cutoff = store.now()
    date = datetime.now(tz()).strftime("%a %d %b")
    internships, graduates = pick_lists(conn, since)
    if not internships and not graduates:
        if not dry_run:
            store.set_state(conn, "broadcast_since", cutoff)
        return {"skipped": "no new jobs since the last list"}
    header = (f"☀️ <b>{date}</b>\n<i>Applied to one? Tap the ✅ /a_… under it. /applied lists them, "
              "/filters narrows this list.</i>")
    link = linker(conn, internships, graduates)
    subscribers = [r for r in store.all_subscribers(conn) if r["active"]]
    if dry_run:
        messages, table = build_messages(conn, internships, graduates, link=link)
        print(f"--- morning list for {len(subscribers)} subscriber(s) (dry run, no filters)")
        for m in [header] + messages:
            print("\n" + m)
        if table:
            print(f"\n[attached {csv_name()}: {len(internships) + len(graduates)} jobs]")
        return {"subscribers": len(subscribers), "sent": 0, "failed": 0, "dry_run": True}
    # Saved before sending: a crash mid-way means some people miss one list, never that everyone gets two.
    store.set_state(conn, "broadcast_since", cutoff)
    store.set_state(conn, "broadcast_day", today)
    watched = store.all_watches(conn)
    lists: dict[str, tuple[list[str], tuple | None]] = {}  # built once per distinct set of filters
    sent = failed = paused = 0
    for sub in subscribers:
        if sub["paused_until"] and sub["paused_until"] > cutoff:
            paused += 1
            continue
        key = sub["filters"] or ""
        if key not in lists:
            filters = json.loads(key) if key else {}
            mine_i, mine_g = apply_filters(internships, graduates, filters)
            if mine_i or mine_g:
                messages, table = build_messages(conn, mine_i, mine_g, link=link)
                caption = f"📄 All {len(mine_i) + len(mine_g)} new jobs" + (" for your /filters" if filters else "")
                lists[key] = (messages, (csv_name(), table, caption))
            else:
                lists[key] = ([f"Nothing new today for your filters ({e(describe_filters(filters))}). "
                               "Change them with /filters."], None)
        messages, document = lists[key]
        alerts = watch_messages(internships, graduates, watched.get(sub["chat_id"], []), link)
        if send_to(conn, sub["chat_id"], [header] + alerts + messages, document):
            sent += 1
        else:
            failed += 1
    return {"subscribers": len(subscribers), "sent": sent, "failed": failed, "paused": paused, "dry_run": False}


def send_jobs_file(conn, chat_id: int, internships: list, graduates: list, filename: str, title: str,
                   label: str, intro: str | None = None, empty: str | None = None) -> bool:
    """A list of jobs as one CSV with the counts in its caption; a short message instead when there are none."""
    [counts], table = build_messages(conn, internships, graduates, label=label, summary_only=True)
    first = [intro] if intro else []
    if not table:
        return send_to(conn, chat_id, first + [empty or f"{title}\n{counts}"])
    return send_to(conn, chat_id, first, (csv_name(filename), table, f"{title}\n{counts}" if title else counts))


def send_latest(conn, chat_id: int, intro: str | None = None) -> bool:
    """The last 2 weeks' jobs (narrowed by the person's /filters) as one CSV file, after an optional intro."""
    filters = store.get_filters(conn, chat_id)
    internships, graduates = apply_filters(*pick_lists(conn), filters)
    title = f"🔎 Your filters: {e(describe_filters(filters))}" if filters else ""
    return send_jobs_file(conn, chat_id, internships, graduates, "latest_jobs", title, "in the last 2 weeks", intro)
