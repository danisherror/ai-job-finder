"""Telegram bot: send messages with Applied/Skipped buttons, and collect button taps."""

import os
import time

import httpx

from src import store
from src.models import Profile

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 30


class TelegramError(Exception):
    pass


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set in .env")
    return token


def api(method: str, **params):
    """Call one Bot API method and return its `result`. Waits and retries once if rate-limited."""
    url = API.format(token=_token(), method=method)
    for attempt in range(3):
        try:
            resp = httpx.post(url, json=params, timeout=TIMEOUT)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            if attempt == 2:
                raise TelegramError(f"{method}: {type(e).__name__}") from None  # message may contain the token
            time.sleep(2 ** (attempt + 1))
            continue
        if data.get("ok"):
            return data["result"]
        retry = (data.get("parameters") or {}).get("retry_after")
        if resp.status_code == 429 and retry and attempt < 2:
            time.sleep(min(int(retry), 60))
            continue
        raise TelegramError(f"{method}: {data.get('description', resp.status_code)}")
    raise TelegramError(f"{method}: gave up")


def decision_buttons(match_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Applied", "callback_data": f"a:{match_id}"},
        {"text": "❌ Skip", "callback_data": f"s:{match_id}"},
    ]]}


def decided_buttons(match_id: int, decision: str) -> dict:
    label = "✅ Applied" if decision == "applied" else "❌ Skipped"
    return {"inline_keyboard": [[{"text": f"{label} · undo", "callback_data": f"u:{match_id}"}]]}


def send_message(chat_id: int, text: str, buttons: dict | None = None, quiet: bool = False) -> int:
    """Send an HTML message; returns Telegram's message ID once it has accepted it."""
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}, "disable_notification": quiet}
    if buttons:
        params["reply_markup"] = buttons
    return api("sendMessage", **params)["message_id"]


# ---------------------------------------------------------------- taps

DECISIONS = {"a": "applied", "s": "skipped", "u": "none"}


def handle_callback(conn, cq: dict, chat_to_profile: dict[int, str]) -> str:
    """Save one button tap. Only the person the job was sent to can change its decision."""
    action, _, raw_id = (cq.get("data") or "").partition(":")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    if action not in DECISIONS or not raw_id.isdigit() or chat_id not in chat_to_profile:
        api("answerCallbackQuery", callback_query_id=cq["id"])
        return "ignored"
    match_id = int(raw_id)
    row = conn.execute("SELECT profile_id FROM matches WHERE id = ?", (match_id,)).fetchone()
    if not row or row["profile_id"] != chat_to_profile[chat_id]:
        api("answerCallbackQuery", callback_query_id=cq["id"])
        return "ignored"
    decision = DECISIONS[action]
    with conn:
        conn.execute("UPDATE matches SET decision = ?, decided_at = ? WHERE id = ?",
                     (decision, store.now() if decision != "none" else None, match_id))
    markup = decision_buttons(match_id) if decision == "none" else decided_buttons(match_id, decision)
    try:
        api("editMessageReplyMarkup", chat_id=chat_id, message_id=cq["message"]["message_id"], reply_markup=markup)
    except TelegramError:
        pass  # the decision is saved; an unchanged button is only cosmetic
    api("answerCallbackQuery", callback_query_id=cq["id"],
        text={"applied": "Marked as applied", "skipped": "Skipped", "none": "Undone"}[decision])
    return decision


def poll_updates(conn, profiles: list[Profile], on_message=None) -> dict:
    """Fetch new updates since the last poll and save any button taps. Returns counts.

    on_message(conn, message) -> bool answers bot commands; without it, /start replies with the chat ID."""
    chat_to_profile = {p.telegram_chat_id: p.id for p in profiles if p.telegram_chat_id}
    offset = int(store.get_state(conn, "telegram_offset") or 0)
    counts = {"taps": 0, "ignored": 0, "messages": 0}
    while True:
        updates = api("getUpdates", offset=offset, timeout=0, allowed_updates=["callback_query", "message"])
        if not updates:
            break
        for u in updates:
            if "callback_query" in u:
                result = handle_callback(conn, u["callback_query"], chat_to_profile)
                counts["ignored" if result == "ignored" else "taps"] += 1
            elif on_message and u.get("message"):
                try:
                    counts["messages"] += bool(on_message(conn, u["message"]))
                except TelegramError:
                    pass  # e.g. they blocked the bot right after writing; don't stop the other updates
            elif (u.get("message") or {}).get("text", "").startswith("/start"):
                # Lets a new person find the chat ID that goes in their profile.
                chat_id = u["message"]["chat"]["id"]
                send_message(chat_id, f"Hi! Your chat ID is <code>{chat_id}</code>. "
                                      "Send it to the person who runs this bot.")
                counts["messages"] += 1
            offset = u["update_id"] + 1
            store.set_state(conn, "telegram_offset", str(offset))
    return counts
