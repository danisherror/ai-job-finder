"""The hosted bot (a Vercel function): Telegram sends each message and button tap here as it happens.

Set it up with `uv run python -m src.main webhook https://<app>.vercel.app/api/telegram`.
"""

import hmac
import json
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler

from src import commands, store, telegram
from src.config import load_profiles


def _authorized(headers) -> bool:
    """Only Telegram knows the secret it was given in setWebhook; anything else is turned away."""
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    return bool(secret) and hmac.compare_digest(headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret)


def _kind(update: dict) -> str:
    """What an update was, for the log: the command word or "button tap", never the person's text."""
    if "callback_query" in update:
        return "button tap"
    text = ((update.get("message") or {}).get("text") or "").strip()
    return text.split()[0].split("@")[0][:20] if text.startswith("/") else "message"


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return
        try:
            update = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            chat_to_profile = {p.telegram_chat_id: p.id for p in load_profiles() if p.telegram_chat_id}
            started = time.monotonic()
            conn = store.connect()
            try:
                telegram.handle_update(conn, update, chat_to_profile, commands.handle_message)
            finally:
                conn.close()
            print(f"answered {_kind(update)} in {time.monotonic() - started:.1f}s")
        except Exception:
            traceback.print_exc()  # logged on Vercel; still answer 200 so Telegram doesn't resend it forever
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Job Finder bot is running.")
