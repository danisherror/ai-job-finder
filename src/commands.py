"""Bot commands a subscriber can send: /start, /latest, /search, /city, /filters, /watch, /applied, /stats, ...

Each command answers right away in the same chat. The daily list itself is sent by broadcast.send_daily.
"""

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from src import broadcast, health, store
from src.broadcast import (GRAD_YEAR, SEARCH_DAYS, apply_filters, describe_filters, pick_lists, send_jobs_file,
                           send_latest, send_to)
from src.digest import _date, e, safe_apply_url, tz, usual_hosts
from src.normalize import indian_cities

MAX_WATCHES = 10
MAX_PAUSE_DAYS = 60
MAX_FEEDBACK_PER_DAY = 5
MAX_SEARCH_WORDS = 5
NOT_SUBSCRIBED = "You're not subscribed. Send /start first."
TYPE_WORDS = {"internships": "internships", "internship": "internships", "intern": "internships",
              "interns": "internships", "jobs": "jobs", "job": "jobs", "graduate": "jobs", "graduates": "jobs",
              "grad": "jobs", str(GRAD_YEAR): "jobs", "both": "both", "all": "both", "any": "both"}


def parse_cities(text: str) -> list[str]:
    """Canonical city names in free text ("bangalore pune" -> Bengaluru, Pune), plus "remote" if mentioned."""
    cities = indian_cities(text)
    if re.search(r"\bremote\b", text, re.I):
        cities.append("remote")
    return list(dict.fromkeys(cities))


def _subscribed(conn, chat_id: int) -> bool:
    row = store.subscriber(conn, chat_id)
    if row and row["active"]:
        return True
    send_to(conn, chat_id, [NOT_SUBSCRIBED])
    return False


# ---------------------------------------------------------------- finding jobs

def cmd_start(conn, chat_id, arg, message):
    chat = message["chat"]
    user = message.get("from") or {}
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or chat.get("title")
    store.subscribe(conn, chat_id, name, user.get("username"))
    send_latest(conn, chat_id, broadcast.WELCOME)


def cmd_stop(conn, chat_id, arg, message):
    store.unsubscribe(conn, chat_id)
    send_to(conn, chat_id, ["You're unsubscribed. Send /start any time to get the daily lists again.\n"
                            "Just need a break? /pause 7 stops the lists for a week instead."])


def cmd_latest(conn, chat_id, arg, message):
    send_latest(conn, chat_id)


def cmd_search(conn, chat_id, arg, message):
    words = [w for w in arg.lower().split() if len(w) > 1][:MAX_SEARCH_WORDS]
    if not words:
        send_to(conn, chat_id, ["Send what to look for after /search, for example:\n"
                                "/search react\n/search google\n/search data intern"])
        return
    internships, graduates = pick_lists(conn, words=words, max_age_days=SEARCH_DAYS, include_backfill=True)
    title = f"🔎 <b>{e(' '.join(words))}</b>"
    send_jobs_file(conn, chat_id, internships, graduates, "search", title, f"in the last {SEARCH_DAYS} days",
                   empty=f"{title}\nNo open internships or {GRAD_YEAR}-graduate jobs match that "
                         f"(last {SEARCH_DAYS} days). Try fewer or other words.")


def cmd_city(conn, chat_id, arg, message):
    cities = parse_cities(arg)
    if not cities:
        send_to(conn, chat_id, ["Send a city after /city, for example:\n/city bangalore\n/city pune hyderabad\n"
                                "/city remote" + (f"\n\nI don't know “{e(arg)}” as a city in India." if arg else "")])
        return
    internships, graduates = apply_filters(
        *pick_lists(conn, max_age_days=SEARCH_DAYS, include_backfill=True), {"cities": cities})
    title = f"📍 <b>{e(', '.join(cities))}</b>"
    send_jobs_file(conn, chat_id, internships, graduates, "city", title, f"in the last {SEARCH_DAYS} days",
                   empty=f"{title}\nNo open internships or {GRAD_YEAR}-graduate jobs there "
                         f"in the last {SEARCH_DAYS} days.")


def my_settings(conn, chat_id: int) -> list[str]:
    """One person's own settings: is their daily list on or paused, their filters, watches and applied count."""
    sub = store.subscriber(conn, chat_id)
    if not sub or not sub["active"]:
        status = "off, you're not subscribed (/start)"
    elif sub["paused_until"] and sub["paused_until"] > store.now():
        until = datetime.fromisoformat(sub["paused_until"]).astimezone(tz()).strftime("%a %d %b")
        status = f"⏸ paused until {until} (/resume)"
    else:
        status = "▶️ on, every morning by 7 AM"
    watching = ", ".join(store.watches(conn, chat_id)) or "no companies"
    applied = len(store.applied_jobs(conn, chat_id))
    return ["<b>👤 You</b>",
            f"📬 Daily list: {status}",
            f"🔎 Filters: {e(describe_filters(store.get_filters(conn, chat_id)))} (/filters)",
            f"🔔 Watching: {e(watching)} (/watch)",
            f"✅ Applied: {applied} job{'s' if applied != 1 else ''} (/applied)"]


def stats_messages(conn, owner: bool = False, chat_id: int | None = None) -> list[str]:
    """Numbers on the jobs of the last 2 weeks, then the person's own settings (with chat_id);
    the owner also sees how subscribers use the bot."""
    internships, graduates = pick_lists(conn)
    rows = internships + [r for r, _ in graduates]
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    new = sum(r["first_seen"] > week_ago for r in rows)
    companies = Counter(r["company"] for r in rows).most_common(5)
    cities = Counter(c for r in rows for c in json.loads(r["cities"] or "[]")).most_common(5)
    remote = sum(r["work_mode"] == "remote" for r in rows)

    def top(pairs):
        return ", ".join(f"{e(name)} ({n})" for name, n in pairs) or "none yet"
    lines = ["📊 <b>Open jobs from the last 2 weeks</b>",
             f"💼 Software internships: {len(internships)}",
             f"🎓 Jobs for {GRAD_YEAR} graduates: {len(graduates)}",
             f"🆕 New in the last 7 days: {new}",
             f"🏠 Remote: {remote}",
             "",
             f"🏢 <b>Most jobs</b>: {top(companies)}",
             f"📍 <b>Top cities</b>: {top(cities)}"]
    if chat_id is not None:
        lines += [""] + my_settings(conn, chat_id)
    if owner:
        subs = store.all_subscribers(conn)
        active = [s for s in subs if s["active"]]
        now = store.now()
        applied = conn.execute("SELECT COUNT(*), COUNT(DISTINCT chat_id) FROM applications").fetchone()
        lines += ["", "<b>Owner only</b>",
                  f"👥 {len(active)} subscribed · {sum(bool(s['paused_until'] and s['paused_until'] > now) for s in active)} paused"
                  f" · {sum(bool(s['filters']) for s in active)} with filters",
                  f"🔔 {conn.execute('SELECT COUNT(*) FROM watches').fetchone()[0]} watched companies",
                  f"✅ {applied[0]} applications marked by {applied[1]} people"]
        lines += [f"   • {e(r['title'])} · {e(r['company'])}: {r['n']}" for r in store.most_applied(conn)]
    return ["\n".join(lines)]


def cmd_stats(conn, chat_id, arg, message):
    send_to(conn, chat_id, stats_messages(conn, owner=chat_id == health.operator_chat(), chat_id=chat_id))


# ---------------------------------------------------------------- the daily list

FILTERS_HELP = ("/filters city bangalore pune · only these cities (add remote for remote jobs)\n"
                "/filters city any · every city again\n"
                f"/filters type internships · or jobs (for {GRAD_YEAR} graduates), or both\n"
                "/filters reset · remove all filters")


def cmd_filters(conn, chat_id, arg, message):
    if not _subscribed(conn, chat_id):
        return
    filters = store.get_filters(conn, chat_id)
    what, _, rest = arg.strip().partition(" ")
    what, rest = what.lower(), rest.strip()
    if what in ("city", "cities"):
        if rest.lower() in ("any", "all", "none", ""):
            filters.pop("cities", None)
        elif cities := parse_cities(rest):
            filters["cities"] = cities
        else:
            send_to(conn, chat_id, [f"I don't know “{e(rest)}” as a city in India. Try /filters city bangalore"])
            return
    elif what == "type":
        if rest.lower() not in TYPE_WORDS:
            send_to(conn, chat_id, ["Send /filters type internships, /filters type jobs or /filters type both"])
            return
        filters["type"] = TYPE_WORDS[rest.lower()]
    elif what in ("reset", "clear", "off"):
        filters = {}
    elif what:
        send_to(conn, chat_id, [FILTERS_HELP])
        return
    if what:
        store.set_filters(conn, chat_id, filters)
        filters = store.get_filters(conn, chat_id)
    head = "✅ Saved. " if what else ""
    send_to(conn, chat_id, [f"{head}<b>Your filters</b>: {e(describe_filters(filters))}\n"
                            "They apply to your daily list and /latest.\n\n" + FILTERS_HELP])


def cmd_pause(conn, chat_id, arg, message):
    if not _subscribed(conn, chat_id):
        return
    days = int(arg) if arg.strip().isdigit() else 7
    days = max(1, min(days, MAX_PAUSE_DAYS))
    until = datetime.now(timezone.utc) + timedelta(days=days)
    store.set_paused_until(conn, chat_id, until.isoformat(timespec="seconds"))
    send_to(conn, chat_id, [f"⏸ No daily lists until <b>{until.astimezone(tz()).strftime('%a %d %b')}</b>.\n"
                            "/resume restarts them sooner. /latest still works any time."])


def cmd_resume(conn, chat_id, arg, message):
    if not _subscribed(conn, chat_id):
        return
    store.set_paused_until(conn, chat_id, None)
    send_to(conn, chat_id, ["▶️ Daily lists are back on from tomorrow morning. /latest shows what you missed."])


def companies_like(conn, name: str, limit: int = 3) -> list[str]:
    """Company names containing `name`, most jobs first, to confirm a /watch spelling."""
    like = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return [r[0] for r in conn.execute(
        """SELECT company FROM jobs WHERE LOWER(company) LIKE ? ESCAPE '\\'
           GROUP BY company ORDER BY COUNT(*) DESC LIMIT ?""", (f"%{like}%", limit))]


def cmd_watch(conn, chat_id, arg, message):
    if not _subscribed(conn, chat_id):
        return
    company = " ".join(arg.lower().split())[:40]
    current = store.watches(conn, chat_id)
    if not company:
        listed = "\n".join(f"• {e(c)}" for c in current) or "none yet"
        send_to(conn, chat_id, [f"<b>Companies you watch</b>\n{listed}\n\n"
                                "/watch google · add one (their new jobs go at the top of your daily list)\n"
                                "/unwatch google · remove one"])
        return
    if len(company) < 2:
        send_to(conn, chat_id, ["Send a longer company name, e.g. /watch google"])
        return
    if company not in current and len(current) >= MAX_WATCHES:
        send_to(conn, chat_id, [f"You can watch up to {MAX_WATCHES} companies. /unwatch one first."])
        return
    store.add_watch(conn, chat_id, company)
    internships, graduates = pick_lists(conn, words=[company], max_age_days=SEARCH_DAYS, include_backfill=True)
    open_now = sum(company in r["company"].lower() for r in internships + [r for r, _ in graduates])
    names = companies_like(conn, company)
    text = f"🔔 Watching <b>{e(company)}</b>. Their new internships and {GRAD_YEAR}-graduate jobs will be at the top of your daily list."
    if names:
        text += f"\nMatches companies like {e(', '.join(names))}."
        if open_now:
            text += f"\n{open_now} open right now: /search {e(company)}"
    else:
        text += "\n⚠️ I haven't seen a company with that name yet, so check the spelling. I'll keep watching."
    send_to(conn, chat_id, [text])


def cmd_unwatch(conn, chat_id, arg, message):
    company = " ".join(arg.lower().split())
    if company and store.remove_watch(conn, chat_id, company):
        send_to(conn, chat_id, [f"Stopped watching <b>{e(company)}</b>."])
    else:
        send_to(conn, chat_id, ["Send the name as /watch lists it, e.g. /unwatch google"])


# ---------------------------------------------------------------- applied

def mark(conn, chat_id: int, job_id: int, applied: bool) -> None:
    job = store.shared_job(conn, job_id)
    if not job:
        send_to(conn, chat_id, [f"I can't find job {job_id}."])
        return
    what = f"<b>{e(job['title'])}</b> · {e(job['company'])}"
    if applied:
        new = store.mark_applied(conn, chat_id, job_id)
        text = f"{'✅ Marked as applied' if new else 'Already marked as applied'}: {what}\nUndo: /u_{job_id} · All of them: /applied"
    else:
        removed = store.unmark_applied(conn, chat_id, job_id)
        text = f"↩️ Removed from your applied list: {what}" if removed else f"That job wasn't in your applied list: {what}"
    send_to(conn, chat_id, [text])


def cmd_applied(conn, chat_id, arg, message):
    rows = store.applied_jobs(conn, chat_id)
    if not rows:
        send_to(conn, chat_id, ["You haven't marked any jobs yet. After you apply to one in the daily list, "
                                "tap the ✅ /a_… under it."])
        return
    hosts = usual_hosts(conn)
    lines = []
    for r in rows:
        url = safe_apply_url(r["apply_url"], r["source_type"], r["board"], r["ats_id"] or "",
                             hosts.get(r["source"], set()))
        closed = " · closed" if r["lifecycle"] == "closed" else ""
        lines.append(f'• <a href="{e(url)}">{e(r["title"])}</a>\n   {e(r["company"])} · applied '
                     f'{_date(r["applied_at"])}{closed} · undo /u_{r["id"]}')
    send_to(conn, chat_id, broadcast.section("✅ Jobs you applied to", lines, len(rows), "marked"))


# ---------------------------------------------------------------- the rest

def cmd_feedback(conn, chat_id, arg, message):
    text = arg.strip()
    if not text:
        send_to(conn, chat_id, ["Write your message after /feedback, e.g.\n/feedback the Google link is broken"])
        return
    today = datetime.now(tz()).date().isoformat()
    key = f"feedback:{chat_id}"
    day, _, count = (store.get_state(conn, key) or "").partition(":")
    count = int(count) if day == today and count.isdigit() else 0
    if count >= MAX_FEEDBACK_PER_DAY:
        send_to(conn, chat_id, ["You've sent a lot of feedback today. Please try again tomorrow."])
        return
    store.set_state(conn, key, f"{today}:{count + 1}")
    user = message.get("from") or {}
    who = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "Someone"
    if user.get("username"):
        who += f" (@{user['username']})"
    health.tell_owner(f"💬 <b>Feedback</b> from {e(who)} · <code>{chat_id}</code>\n{e(text[:3000])}")
    send_to(conn, chat_id, ["Thanks! Your message was sent."])


def cmd_id(conn, chat_id, arg, message):
    send_to(conn, chat_id, [f"Your chat ID is <code>{chat_id}</code>. Send it to the person who runs this bot."])


def cmd_help(conn, chat_id, arg, message):
    send_to(conn, chat_id, [broadcast.HELP])


def cmd_active(conn, chat_id, arg, message):
    send_to(conn, chat_id, broadcast.subscribers_messages(conn))


COMMANDS = {"/start": cmd_start, "/stop": cmd_stop, "/latest": cmd_latest, "/search": cmd_search,
            "/city": cmd_city, "/stats": cmd_stats, "/filters": cmd_filters, "/filter": cmd_filters,
            "/pause": cmd_pause, "/resume": cmd_resume, "/watch": cmd_watch, "/unwatch": cmd_unwatch,
            "/applied": cmd_applied, "/feedback": cmd_feedback, "/id": cmd_id, "/help": cmd_help}
OWNER_ONLY = {"/active": cmd_active}


def handle_message(conn, message: dict) -> bool:
    """Answer one bot command. Returns True if it was a command this bot knows."""
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    parts = text.split(None, 1)
    command = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    chat_id = message["chat"]["id"]
    if m := re.fullmatch(r"/([au])_(\d{1,9})", command):  # tapped ✅ /a_12 under a job, or /u_12 to undo
        mark(conn, chat_id, int(m[2]), applied=m[1] == "a")
        return True
    handler = COMMANDS.get(command)
    if not handler and chat_id == health.operator_chat():
        handler = OWNER_ONLY.get(command)
    if not handler:
        if command not in OWNER_ONLY:  # others get no hint that owner commands exist
            send_to(conn, chat_id, ["I don't know that command. /help lists what I can do."])
        return False
    handler(conn, chat_id, arg, message)
    return True
