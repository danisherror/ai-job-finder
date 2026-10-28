"""The shared lists every bot subscriber gets: software internships and jobs for 2027 graduates in India.

Anyone who presses Start in the bot is subscribed. No profile and no AI: everyone gets the same lists, built
from the same rules as private/exports/india_internships.csv and graduates_2027.csv. An internship goes only
in the internship list, never in both.
"""

import csv
import io
import time
from datetime import datetime, timedelta, timezone

from src import store, telegram
from src.digest import (GRAD_YEAR, _date, e, grad_reason_order, graduate_reason, is_india_internship, place,
                        safe_apply_url, tz, usual_hosts)

MAX_AGE_DAYS = 14     # leave out postings older than this, even when a newly added source finds them today
MESSAGE_LIMIT = 3800  # Telegram allows 4096 characters per message
SEND_PAUSE = 0.5      # seconds between messages; stays under Telegram's rate limits

SHORT_REASON = {f"{GRAD_YEAR} batch": f"🎯 {GRAD_YEAR} batch", "campus / graduate / fresher role": "campus / fresher",
                "fresher role (0 years)": "0 years"}

WELCOME = ("👋 <b>Welcome!</b> You're subscribed.\n\n"
           "Every morning by 7 AM IST you'll get the new <b>software internships</b> and "
           f"<b>jobs for {GRAD_YEAR} graduates</b> open to people in India, from company career sites, "
           "job boards and Indian job portals.\n\n"
           "/latest shows the newest jobs any time\n/id shows your chat ID\n/stop unsubscribes")
HELP = ("I send new software internships and jobs for "
        f"{GRAD_YEAR} graduates in India every morning by 7 AM IST.\n\n"
        "/latest shows the newest jobs\n/id shows your chat ID\n/stop unsubscribes\n/start subscribes again")
COMMANDS = [{"command": "latest", "description": "Newest internships and 2027-graduate jobs"},
            {"command": "id", "description": "Show your chat ID"},
            {"command": "stop", "description": "Stop the daily lists"},
            {"command": "start", "description": "Subscribe to the daily lists"},
            {"command": "help", "description": "What this bot does"}]


# ---------------------------------------------------------------- picking

def _when(r) -> datetime | None:
    value = r["posted_at"] or r["first_seen"]
    try:
        d = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pick_lists(conn, since: str | None = None, now: datetime | None = None) -> tuple[list, list]:
    """(internships, [(job, reason)] for graduates), newest first; graduates grouped by reason.
    With `since`, only jobs first seen after it."""
    now = now or datetime.now(timezone.utc)
    oldest = now - timedelta(days=MAX_AGE_DAYS)
    sql = """SELECT j.*, s.type AS source_type, s.board FROM jobs j JOIN sources s ON s.name = j.source
             WHERE j.backfill = 0 AND j.lifecycle = 'open' AND j.india_eligible = 'yes'"""
    params: tuple = ()
    if since:
        sql += " AND j.first_seen > ?"
        params = (since,)
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


# ---------------------------------------------------------------- formatting

def job_line(r, url: str, tag: str | None = None) -> str:
    where = e(place(r))
    if extra := (r["more_places"] if "more_places" in r.keys() else 0):
        where += f" (+{extra} more place{'s' if extra > 1 else ''})"
    details = " · ".join(x for x in (e(r["company"]), where, tag) if x)
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


def jobs_csv(internships: list, graduates: list, url) -> bytes:
    """Both lists in one spreadsheet, internships first."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["list", "posted", "company", "title", "location", "why", "apply_url"])
    rows = [(r, "internship", "") for r in internships] + [(r, f"{GRAD_YEAR} graduates", why) for r, why in graduates]
    for r, kind, why in rows:
        where = place(r)
        if extra := (r["more_places"] if "more_places" in r.keys() else 0):
            where += f" (+{extra} more)"
        w.writerow([kind, _date(r["posted_at"] or r["first_seen"]), r["company"], r["title"], where, why, url(r)])
    return out.getvalue().encode("utf-8-sig")  # -sig: Excel then reads ₹, é, etc. correctly


def summary(internships: list, graduates: list, label: str) -> str:
    """Counts only, for a message that comes with the CSV instead of the job list."""
    if not internships and not graduates:
        return f"Nothing {label} yet. Your first list arrives tomorrow morning."
    return (f"<b>💼 Software internships</b> · {len(internships)} {label}\n"
            f"<b>🎓 Jobs for {GRAD_YEAR} graduates</b> · {len(graduates)} {label}\n\n"
            "📄 Every job is in the CSV file below.")


def build_messages(conn, since: str | None, label: str = "new today",
                   summary_only: bool = False) -> tuple[list[str], bytes | None]:
    """The chat messages listing every job (or, with summary_only, just the counts),
    plus a CSV of them all (None when there are no jobs)."""
    internships, graduates = pick_lists(conn, since)
    hosts = usual_hosts(conn)

    def url(r):
        return safe_apply_url(r["apply_url"], r["source_type"], r["board"], r["ats_id"] or "",
                              hosts.get(r["source"], set()))

    table = jobs_csv(internships, graduates, url) if internships or graduates else None
    if summary_only:
        return [summary(internships, graduates, label)], table
    intern_lines = [job_line(r, url(r)) for r in internships]
    grad_lines = [job_line(r, url(r), SHORT_REASON.get(reason)) for r, reason in graduates]
    messages = (section("💼 Software internships", intern_lines, len(internships), label)
                + section(f"🎓 Jobs for {GRAD_YEAR} graduates", grad_lines, len(graduates), label))
    return messages, table


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
    """Send the jobs first seen since the last list to every subscriber.

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
    messages, table = build_messages(conn, since)
    messages = [f"☀️ <b>{date}</b>"] + messages
    document = (csv_name(), table, f"📄 All {len(internships) + len(graduates)} new jobs") if table else None
    chats = store.active_subscribers(conn)
    if dry_run:
        print(f"--- morning list for {len(chats)} subscriber(s) (dry run)")
        for m in messages:
            print("\n" + m)
        if document:
            print(f"\n[attached {document[0]}: {document[2]}]")
        return {"subscribers": len(chats), "sent": 0, "failed": 0, "dry_run": True}
    # Saved before sending: a crash mid-way means some people miss one list, never that everyone gets two.
    store.set_state(conn, "broadcast_since", cutoff)
    store.set_state(conn, "broadcast_day", today)
    sent = failed = 0
    for chat in chats:
        if send_to(conn, chat, messages, document):
            sent += 1
        else:
            failed += 1
    return {"subscribers": len(chats), "sent": sent, "failed": failed, "dry_run": False}


def send_latest(conn, chat_id: int, intro: str | None = None, summary_only: bool = False) -> bool:
    messages, table = build_messages(conn, None, label="in the last 2 weeks", summary_only=summary_only)
    document = (csv_name("latest_jobs"), table, "📄 All jobs from the last 2 weeks") if table else None
    return send_to(conn, chat_id, ([intro] if intro else []) + messages, document)


def handle_message(conn, message: dict) -> bool:
    """Answer one bot command. Returns True if it was a command this bot knows."""
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command = text.split()[0].split("@")[0].lower()
    chat = message["chat"]
    chat_id = chat["id"]
    if command == "/start":
        user = message.get("from") or {}
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or chat.get("title")
        store.subscribe(conn, chat_id, name, user.get("username"))
        send_latest(conn, chat_id, WELCOME, summary_only=True)  # a new subscriber gets the file, not 30 messages
    elif command == "/stop":
        store.unsubscribe(conn, chat_id)
        send_to(conn, chat_id, ["You're unsubscribed. Send /start any time to get the daily lists again."])
    elif command == "/latest":
        send_latest(conn, chat_id)
    elif command == "/id":
        send_to(conn, chat_id, [f"Your chat ID is <code>{chat_id}</code>. "
                                "Send it to the person who runs this bot."])
    elif command == "/help":
        send_to(conn, chat_id, [HELP])
    else:
        return False
    return True
