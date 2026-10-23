"""Each person's morning list: pick, format, send, mark sent, export CSV."""

import csv
import html
import json
import os
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from src import store, telegram
from src.config import ROOT
from src.models import Profile

EXPORTS = ROOT / "private" / "exports"
FIRST_LIST_MAX = 10
FIT_ORDER = {"strong": 0, "good": 1, "weak": 2}
FIT_LABEL = {"strong": "🟢 Strong fit", "good": "🟡 Good fit", "weak": "⚪ Weak fit (filling a slot)"}
ATS_DOMAINS = {"greenhouse": ("greenhouse.io",), "lever": ("lever.co",), "ashby": ("ashbyhq.com",)}
ATS_JOB_PAGE = {
    "greenhouse": "https://job-boards.greenhouse.io/{board}/jobs/{id}",
    "lever": "https://jobs.lever.co/{board}/{id}",
    "ashby": "https://jobs.ashbyhq.com/{board}/{id}",
}


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ") or "Asia/Kolkata")


# ---------------------------------------------------------------- picking

def pending(conn, profile_id: str) -> list:
    """Scored matches not yet delivered (or whose sending failed), for jobs that are still open.
    Jobs the AI rated "no" (a different kind of work) are never sent, not even to fill the list; they stay in the CSV."""
    return conn.execute(
        """SELECT m.*, j.title, j.company, j.location_raw, j.cities, j.work_mode, j.apply_url, j.ats_id,
                  j.posted_at, j.first_seen, j.source, s.type AS source_type, s.board
           FROM matches m JOIN jobs j ON j.job_key = m.job_key JOIN sources s ON s.name = j.source
           WHERE m.profile_id = ? AND m.fit != 'no' AND m.delivery IN ('pending', 'failed') AND m.decision = 'none'
             AND j.lifecycle = 'open'""",
        (profile_id,),
    ).fetchall()


def pick(rows: list, size: int) -> tuple[list, list]:
    """Best first: strong, then good, weak only to fill. Newest first within a level. Returns (today, overflow)."""
    ordered = sorted(rows, key=lambda r: (FIT_ORDER[r["fit"]], -_ts(r["posted_at"] or r["first_seen"])))
    return ordered[:size], ordered[size:]


def _ts(value: str | None) -> float:
    try:
        return datetime.fromisoformat(value).timestamp() if value else 0
    except ValueError:
        return 0


def list_size(conn, profile: Profile) -> int:
    ever_sent = conn.execute("SELECT 1 FROM matches WHERE profile_id = ? AND delivery = 'sent' LIMIT 1",
                             (profile.id,)).fetchone()
    return profile.size if ever_sent else min(profile.size, FIRST_LIST_MAX)


# ---------------------------------------------------------------- links

def usual_hosts(conn) -> dict[str, set[str]]:
    """Per source, the apply-link hosts used by at least 20% of its jobs (e.g. stripe.com for Stripe)."""
    rows = conn.execute("SELECT source, apply_url FROM jobs").fetchall()
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        host = urlsplit(r["apply_url"]).hostname or ""
        counts.setdefault(r["source"], {}).setdefault(host, 0)
        counts[r["source"]][host] += 1
    out = {}
    for source, hosts in counts.items():
        total = sum(hosts.values())
        out[source] = {h for h, n in hosts.items() if n >= 0.2 * total}
    return out


def safe_apply_url(url: str, source_type: str, board: str, ats_id: str, allowed_hosts: set[str]) -> str:
    """The job's own link if it is https on the ATS or the company's usual host; else the plain ATS job page."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    on_ats = any(host == d or host.endswith("." + d) for d in ATS_DOMAINS.get(source_type, ()))
    if parts.scheme == "https" and (on_ats or host in allowed_hosts):
        return url
    return ATS_JOB_PAGE[source_type].format(board=board, id=ats_id)


# ---------------------------------------------------------------- formatting

def e(text) -> str:
    return html.escape(str(text), quote=False)


def _list(raw: str | None, limit: int = 6) -> str:
    items = json.loads(raw or "[]")[:limit]
    return ", ".join(e(i) for i in items)


def place(row) -> str:
    cities = json.loads(row["cities"] or "[]")
    where = ", ".join(cities) if cities else (row["location_raw"] or "")
    if len(where) > 80:
        where = where[:77] + "…"
    mode = row["work_mode"] if row["work_mode"] != "unknown" else ""
    return " · ".join(x for x in (where, mode) if x)


def job_message(row, url: str) -> str:
    lines = [f"<b>{e(row['title'])}</b>", f"{e(row['company'])} · {e(place(row))}", FIT_LABEL[row["fit"]]]
    if matched := _list(row["matched_skills"]):
        lines.append(f"✓ {matched}")
    if missing := _list(row["missing_skills"]):
        lines.append(f"✗ Missing: {missing}")
    if flags := _list(row["red_flags"], 3):
        lines.append(f"⚠ {flags}")
    if row["reason"]:
        lines.append(f"<i>{e(row['reason'])}</i>")
    lines.append(f'<a href="{html.escape(url, quote=True)}">Apply</a>')
    return "\n".join(lines)


def scan_footer(conn, run_id: str | None) -> str:
    if not run_id:
        return "No collection run yet"
    ok, total = conn.execute(
        "SELECT COALESCE(SUM(result = 'ok'), 0), COUNT(*) FROM source_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    started = conn.execute("SELECT started FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    at = datetime.fromisoformat(started[0]).astimezone(tz()).strftime("%H:%M") if started else "?"
    return f"Scanned {ok} of {total} sources · run at {at}"


def header(count: int, today: list) -> str:
    date = datetime.now(tz()).strftime("%a %d %b")
    if not count:
        return f"<b>{date}</b>\nNo new matches today."
    by_fit = {f: sum(r["fit"] == f for r in today) for f in FIT_ORDER}
    parts = [f"{n} {f}" for f, n in by_fit.items() if n]
    return f"<b>{date}</b> · {count} new match{'es' if count > 1 else ''} ({', '.join(parts)})"


# ---------------------------------------------------------------- export

def export_csv(conn, profile: Profile) -> None:
    """Rewrite private/exports/<person>.csv with every match: sent, overflow and not yet sent."""
    EXPORTS.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        """SELECT m.scored_at, m.fit, j.title, j.company, j.location_raw, j.work_mode, m.delivery, m.decision,
                  j.apply_url, m.reason, m.matched_skills, m.missing_skills, j.lifecycle
           FROM matches m JOIN jobs j ON j.job_key = m.job_key
           WHERE m.profile_id = ? ORDER BY m.scored_at DESC""",
        (profile.id,),
    ).fetchall()
    with open(EXPORTS / f"{profile.id}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scored_at", "fit", "title", "company", "location", "work_mode", "delivery", "decision",
                    "apply_url", "reason", "matched_skills", "missing_skills", "job_status"])
        for r in rows:
            r = dict(r)
            r["matched_skills"] = "; ".join(json.loads(r["matched_skills"] or "[]"))
            r["missing_skills"] = "; ".join(json.loads(r["missing_skills"] or "[]"))
            w.writerow(r.values())


# ---------------------------------------------------------------- send

def send_digest(conn, profile: Profile, run_id: str | None, dry_run: bool = False) -> dict:
    """Send today's list. A match is marked `sent` only after Telegram accepts its message."""
    today, overflow = pick(pending(conn, profile.id), list_size(conn, profile))
    hosts = usual_hosts(conn)
    messages = [(r, job_message(r, safe_apply_url(r["apply_url"], r["source_type"], r["board"], r["ats_id"],
                                                  hosts.get(r["source"], set()))))
                for r in today]
    head, foot = header(len(today), today), scan_footer(conn, run_id)

    if dry_run or not profile.telegram_chat_id:
        print(f"--- {profile.name} ({'dry run' if dry_run else 'no telegram_chat_id; not sent'})")
        print(head)
        for _, text in messages:
            print("\n" + text)
        print("\n" + foot)
        export_csv(conn, profile)
        return {"sent": 0, "failed": 0, "overflow": len(overflow), "dry_run": True}

    chat = profile.telegram_chat_id
    telegram.send_message(chat, head)  # if this fails, nothing is marked and everything stays pending
    sent = failed = 0
    for row, text in messages:
        try:
            msg_id = telegram.send_message(chat, text, telegram.decision_buttons(row["id"]), quiet=True)
        except telegram.TelegramError:
            failed += 1
            with conn:
                conn.execute("UPDATE matches SET delivery = 'failed' WHERE id = ?", (row["id"],))
            continue
        sent += 1
        with conn:
            conn.execute("UPDATE matches SET delivery = 'sent', sent_at = ?, telegram_message_id = ? WHERE id = ?",
                         (store.now(), msg_id, row["id"]))
        time.sleep(0.5)  # stay well under Telegram's per-chat rate limit
    with conn:  # no carry-over: what didn't fit today is only in the CSV
        conn.executemany("UPDATE matches SET delivery = 'overflow' WHERE id = ?", [(r["id"],) for r in overflow])
    try:
        telegram.send_message(chat, foot + (f"\n⚠ {failed} job(s) failed to send; will retry tomorrow" if failed else ""),
                              quiet=True)
    except telegram.TelegramError:
        pass  # the jobs themselves were delivered
    export_csv(conn, profile)
    return {"sent": sent, "failed": failed, "overflow": len(overflow), "dry_run": False}
