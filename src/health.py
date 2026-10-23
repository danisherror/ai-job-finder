"""Messages to the owner: immediate alerts when something breaks, and a weekly health summary.

Both go to OPERATOR_TELEGRAM_CHAT_ID. If that isn't set or Telegram is down, they are printed to the log instead,
so a failure is never silent.
"""

import html
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from src import rules, store, telegram
from src.models import Profile

HEALTH_EVERY_DAYS = 7
STALE_RUN_HOURS = 26     # no finished run for this long → alert (checked by the hourly job)


def e(text) -> str:
    return html.escape(str(text), quote=False)


def operator_chat() -> int | None:
    raw = os.environ.get("OPERATOR_TELEGRAM_CHAT_ID", "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def tell_owner(text: str) -> bool:
    """Send to the owner; print instead if that isn't possible. Never raises. Returns True if Telegram took it."""
    chat = operator_chat()
    if chat:
        try:
            telegram.send_message(chat, text)
            return True
        except telegram.TelegramError as err:
            print(f"Could not message the owner ({err}). Message was:")
    else:
        print("OPERATOR_TELEGRAM_CHAT_ID is not set. Message for the owner:")
    print(text)
    return False


def alert(conn, key: str, text: str) -> None:
    """An immediate alert, sent at most once per day for the same `key` (e.g. 'claude_logged_out')."""
    today = datetime.now(timezone.utc).date().isoformat()
    state_key = f"alert:{key}"
    if store.get_state(conn, state_key) == today:
        print(f"(alert '{key}' already sent today) {text}")
        return
    if tell_owner("⚠️ <b>Job Finder</b>\n" + text):
        store.set_state(conn, state_key, today)


# ---------------------------------------------------------------- alerts the daily run raises

AI_ALERTS = {
    "logged_out": "Claude Code is logged out, so no jobs were scored. Run <code>claude</code> and log in.",
    "not_installed": "<code>claude</code> was not found on PATH, so no jobs were scored.",
    "usage_limit": "The Claude usage limit was reached; unscored jobs wait for the next run.",
}


def check_run(conn, run_id: str, scanned: int, total: int, match_results: dict, send_failures: dict) -> None:
    """Alert on anything in today's run that the owner has to act on."""
    if total and not scanned:
        reasons = Counter(r[0] for r in conn.execute(
            "SELECT error_reason FROM source_runs WHERE run_id = ? AND result != 'ok'", (run_id,)))
        alert(conn, "run_failed", f"No source could be scanned ({_counts(reasons)}). Is the Mac online?")
    stopped = {r["stopped"] for r in match_results.values() if r.get("stopped")}
    for reason in sorted(stopped):
        alert(conn, f"ai_{reason}", AI_ALERTS.get(reason, f"AI scoring stopped: {e(reason)}"))
    for profile_id, err in send_failures.items():
        alert(conn, f"send_{profile_id}", f"Today's list for <b>{e(profile_id)}</b> could not be sent: {e(err)}")


def check_stale(conn) -> None:
    """Called hourly: alert if the daily run hasn't finished for over a day (Mac off, schedule broken, crash)."""
    row = conn.execute("SELECT MAX(finished) FROM runs WHERE state = 'done'").fetchone()
    if not row or not row[0]:
        return  # never ran yet: nothing to compare with
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row[0])
    if age > timedelta(hours=STALE_RUN_HOURS):
        alert(conn, "stale", f"The daily run hasn't finished for {age.days}d {age.seconds // 3600}h. "
                             "Check <code>private/logs/daily.log</code>.")


def _counts(counter: Counter) -> str:
    return ", ".join(f"{k} ×{n}" if n > 1 else str(k) for k, n in counter.most_common()) or "no details"


# ---------------------------------------------------------------- weekly summary

def summary(conn, profiles: list[Profile], days: int = HEALTH_EVERY_DAYS) -> str:
    """The weekly health message (Telegram HTML)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    lines = [f"🩺 <b>Job Finder · last {days} days</b>"]

    runs = conn.execute("SELECT state FROM runs WHERE started >= ?", (since,)).fetchall()
    states = Counter(r[0] for r in runs)
    lines.append(f"Runs: {len(runs)}" + (f" ({_counts(states)})" if runs else " ⚠ none"))

    # Per source, over the period. A source with no row at all is shown too: it was never even tried.
    per_source: dict[str, Counter] = defaultdict(Counter)
    reasons: dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute("SELECT source, result, error_reason FROM source_runs WHERE started >= ?", (since,)):
        per_source[r["source"]][r["result"]] += 1
        if r["result"] != "ok":
            reasons[r["source"]][r["error_reason"] or r["result"]] += 1
    active = [r[0] for r in conn.execute("SELECT name FROM sources WHERE active = 1 ORDER BY name")]
    total_ok = sum(c["ok"] for c in per_source.values())
    total = sum(sum(c.values()) for c in per_source.values())
    rate = f"{100 * total_ok / total:.0f}%" if total else "n/a"
    problems = []
    for name in active:
        c = per_source.get(name, Counter())
        n = sum(c.values())
        if c["ok"] < n or (runs and not n):
            detail = _counts(reasons[name]) if n else "not scanned"
            problems.append(f" • {e(name)}: {c['ok']}/{n} ok — {e(detail)}")
    lines.append(f"Sources: {len(active) - len(problems)} of {len(active)} always fine · scan success {rate}")
    lines += problems

    usual_zero = conn.execute(
        "SELECT name FROM sources WHERE active = 1 AND first_scan_at IS NOT NULL AND usual_count = 0").fetchall()
    if usual_zero:
        lines.append("Now listing 0 jobs: " + ", ".join(e(r[0]) for r in usual_zero))

    calls = conn.execute("SELECT COUNT(*), COALESCE(SUM(error_reason IS NOT NULL), 0), AVG(seconds) "
                         "FROM llm_calls WHERE at >= ?", (since,)).fetchone()
    if calls[0]:
        errs = Counter(r[0] for r in conn.execute(
            "SELECT error_reason FROM llm_calls WHERE at >= ? AND error_reason IS NOT NULL", (since,)))
        lines.append(f"AI: {calls[0]} calls, {calls[1]} failed" + (f" ({_counts(errs)})" if errs else "")
                     + f", {calls[2]:.0f}s average")
    else:
        lines.append("AI: no calls")

    for p in profiles:
        backlog = sum(1 for _, job in store.unscored_jobs(conn, p) if rules.passes_rules(job, p)[0])
        sent = conn.execute("SELECT COUNT(*), COALESCE(SUM(decision = 'applied'), 0) FROM matches "
                            "WHERE profile_id = ? AND sent_at >= ?", (p.id, since)).fetchone()
        lines.append(f"{e(p.name)}: {sent[0]} jobs sent, {sent[1]} applied, {backlog} waiting to be scored")
    return "\n".join(lines)


def weekly_if_due(conn, profiles: list[Profile]) -> bool:
    """Send the summary when a week has passed since the last one. The first run only starts the clock."""
    today = datetime.now(timezone.utc).date()
    last = store.get_state(conn, "health_last_sent")
    if last is None:
        store.set_state(conn, "health_last_sent", today.isoformat())
        return False
    if (today - datetime.fromisoformat(last).date()).days < HEALTH_EVERY_DAYS:
        return False
    if tell_owner(summary(conn, profiles)):
        store.set_state(conn, "health_last_sent", today.isoformat())
        return True
    return False
