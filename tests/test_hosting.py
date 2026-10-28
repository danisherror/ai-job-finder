"""Hosting pieces: the webhook bot, the Turso row type, and batched job saving."""

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import libsql
import pytest

from api import telegram as webhook
from src import store, telegram
from src.models import Job, Source


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    db = tmp_path / "w.db"
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda path=None: real_connect(db))
    seen = []
    monkeypatch.setattr(telegram, "handle_update", lambda conn, u, *a, **k: seen.append(u))
    httpd = HTTPServer(("127.0.0.1", 0), webhook.handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/", seen
    httpd.shutdown()


def _post(url, body, secret=None):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


def test_webhook_turns_away_requests_without_the_secret(server):
    url, seen = server
    assert _post(url, {"update_id": 1}) == 401
    assert _post(url, {"update_id": 1}, secret="wrong") == 401
    assert seen == []


def test_webhook_answers_telegram(server):
    url, seen = server
    assert _post(url, {"update_id": 7, "message": {"text": "/latest"}}, secret="s3cret") == 200
    assert seen == [{"update_id": 7, "message": {"text": "/latest"}}]


def test_turso_rows_read_like_sqlite_rows(tmp_path):
    conn = store.TursoConnection(libsql.connect(str(tmp_path / "r.db")))
    row = conn.execute("SELECT 1 AS a, 'x' AS b").fetchone()
    assert (row[0], row["a"], row["b"], row.keys(), dict(row)) == (1, 1, "x", ["a", "b"], {"a": 1, "b": "x"})
    assert [tuple(r) for r in conn.execute("SELECT 2 AS a UNION ALL SELECT 3")] == [(2,), (3,)]
    assert conn.execute("SELECT 1 WHERE 0").fetchone() is None


def test_jobs_saved_in_many_small_batches_match_one_batch(tmp_path, monkeypatch):
    jobs = [Job(source="Acme", ats_id=str(i), title=f"Engineer {i}", company="Acme", location_raw="Bengaluru",
                description="x" * 300 if i % 2 else "", apply_url=f"https://acme.com/jobs/{i}") for i in range(40)]
    source = Source(name="Acme", type="greenhouse", board="acme")

    def save(path):
        conn = store.connect(path)
        store.sync_sources(conn, [source])
        run = store.start_run(conn)
        first = store.save_source_result(conn, run, source, store.now(), jobs)
        again = store.save_source_result(conn, run, source, store.now(), jobs[:30])
        rows = conn.execute("SELECT job_key, title, description, missed_scans, lifecycle FROM jobs ORDER BY job_key")
        return first, again, [tuple(r) for r in rows]

    whole = save(tmp_path / "one.db")
    monkeypatch.setattr(store, "BATCH_BYTES", 200)  # about one job per statement
    assert save(tmp_path / "many.db") == whole
    assert whole[0] == 40 and whole[1] == 0
