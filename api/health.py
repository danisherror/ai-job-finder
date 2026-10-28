"""Daily check (a Vercel cron job): alert the owner on Telegram if the daily GitHub run stopped finishing."""

import hmac
import os
from http.server import BaseHTTPRequestHandler

from src import health, store


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Vercel sends "Authorization: Bearer <CRON_SECRET>" with its cron calls.
        secret = os.environ.get("CRON_SECRET", "")
        if not secret or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {secret}"):
            self.send_response(401)
            self.end_headers()
            return
        conn = store.connect()
        try:
            health.check_stale(conn)
        finally:
            conn.close()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"checked")
