"""The shared internship and 2027-graduate lists sent to every bot subscriber, with Telegram faked."""

from datetime import datetime, timedelta, timezone

import pytest

from src import broadcast, store, telegram
from tests.test_match_and_digest import SRC, make_job


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = store.connect(tmp_path / "t.db")
    store.sync_sources(c, [SRC])
    jobs = [
        make_job(1, title="Software Engineering Intern"),
        make_job(2, title="2027 Campus Hire - Software Engineer"),
        make_job(3, title="Backend Engineer, Team C"),                       # neither list
        make_job(4, title="Backend Intern", india_eligible="no"),            # not open to India
        make_job(5, title="ML Intern", posted_at=datetime.now(timezone.utc) - timedelta(days=30)),  # too old
    ]
    store.save_source_result(c, store.start_run(c), SRC, store.now(), jobs)
    c.execute("UPDATE jobs SET backfill = 0")
    c.commit()
    monkeypatch.setattr(broadcast.time, "sleep", lambda s: None)
    monkeypatch.setattr(telegram, "send_document", lambda *a, **k: 1)  # tests that check the file replace this
    return c


class FakeSend:
    def __init__(self, fail_for: dict[int, str] | None = None):
        self.sent, self.fail_for = [], fail_for or {}

    def __call__(self, chat_id, text, buttons=None, quiet=False):
        if chat_id in self.fail_for:
            raise telegram.TelegramError(self.fail_for[chat_id])
        self.sent.append((chat_id, text))
        return 1


def test_lists_split_internships_from_graduate_jobs(conn):
    internships, graduates = broadcast.pick_lists(conn)
    assert [r["title"] for r in internships] == ["Software Engineering Intern"]
    assert [(r["title"], why) for r, why in graduates] == [("2027 Campus Hire - Software Engineer", "2027 batch")]


def test_morning_list_goes_to_every_subscriber_once_a_day(conn, monkeypatch):
    fake = FakeSend(fail_for={3: "sendMessage: Forbidden: bot was blocked by the user"})
    monkeypatch.setattr(telegram, "send_message", fake)
    for chat in (1, 2, 3):
        store.subscribe(conn, chat, f"P{chat}", None)

    r = broadcast.send_daily(conn)
    assert (r["subscribers"], r["sent"], r["failed"]) == (3, 2, 1)
    assert {chat for chat, _ in fake.sent} == {1, 2}
    text = "\n".join(t for chat, t in fake.sent if chat == 1)
    assert "Software Engineering Intern" in text and "2027 Campus Hire" in text and "Team C" not in text
    assert store.active_subscribers(conn) == [1, 2]                 # blocked the bot: unsubscribed
    assert broadcast.send_daily(conn) == {"skipped": "already sent today"}


def test_next_morning_only_has_jobs_first_seen_since(conn, monkeypatch):
    monkeypatch.setattr(telegram, "send_message", FakeSend())
    store.set_state(conn, "broadcast_since", store.now())
    msgs, table = broadcast.build_messages(conn, store.get_state(conn, "broadcast_since"))
    assert table is None
    assert "Nothing new today" in msgs[0] and "Nothing new today" in msgs[1]


def test_start_subscribes_and_stop_unsubscribes(conn, monkeypatch):
    fake = FakeSend()
    monkeypatch.setattr(telegram, "send_message", fake)
    msg = {"text": "/start", "chat": {"id": 42}, "from": {"first_name": "Riya", "username": "riya"}}
    assert broadcast.handle_message(conn, msg)
    assert store.active_subscribers(conn) == [42]
    assert "Welcome" in fake.sent[0][1]
    assert len(fake.sent) == 2 and "Software internships</b> · 1" in fake.sent[1][1]   # counts, not the jobs
    assert "Software Engineering Intern" not in fake.sent[1][1]

    assert broadcast.handle_message(conn, {"text": "/stop@Bot_Jobfinder_bot", "chat": {"id": 42}})
    assert store.active_subscribers(conn) == []
    assert not broadcast.handle_message(conn, {"text": "hello", "chat": {"id": 42}})


def test_long_lists_are_split_under_the_message_limit():
    lines = [f"• job {i} " + "x" * 300 for i in range(40)]
    msgs = broadcast.section("Internships", lines, 40)
    assert len(msgs) > 1 and all(len(m) <= 4096 for m in msgs)
    assert all(f"job {i} " in "".join(msgs) for i in range(40))


def test_same_posting_in_several_cities_is_listed_once(conn):
    more = [make_job(10 + i, title="Software Engineering Intern", location_raw=city, cities=[city])
            for i, city in enumerate(["Pune", "Hyderabad"])]
    store.save_source_result(conn, store.start_run(conn), SRC, store.now(), more)
    conn.execute("UPDATE jobs SET backfill = 0, lifecycle = 'open'")  # this scan didn't list the first jobs
    conn.commit()
    internships, _ = broadcast.pick_lists(conn)
    assert len(internships) == 1
    assert "(+2 more places)" in broadcast.job_line(internships[0], "https://x.test/1")


def test_share_can_run_twice_a_day_but_sends_each_job_once(conn, monkeypatch):
    fake = FakeSend()
    monkeypatch.setattr(telegram, "send_message", fake)
    store.subscribe(conn, 1, "P1", None)
    assert broadcast.send_daily(conn, once_a_day=False)["sent"] == 1
    sent = len(fake.sent)
    assert broadcast.send_daily(conn, once_a_day=False) == {"skipped": "no new jobs since the last list"}
    assert len(fake.sent) == sent


def test_every_list_comes_with_a_csv_of_every_job(conn, monkeypatch):
    fake, files = FakeSend(), []
    monkeypatch.setattr(telegram, "send_message", fake)
    monkeypatch.setattr(telegram, "send_document",
                        lambda chat_id, name, content, caption="", quiet=False: files.append((chat_id, name, content)))
    store.subscribe(conn, 1, "P1", None)

    assert broadcast.send_daily(conn)["sent"] == 1
    [(chat, name, content)] = files
    rows = content.decode("utf-8-sig").splitlines()
    assert chat == 1 and name.endswith(".csv") and rows[0].startswith("list,posted,company,title")
    assert len(rows) == 3
    assert "Software Engineering Intern" in rows[1] and "2027 Campus Hire" in rows[2]


def test_id_replies_with_the_chat_id(conn, monkeypatch):
    fake = FakeSend()
    monkeypatch.setattr(telegram, "send_message", fake)
    assert broadcast.handle_message(conn, {"text": "/id", "chat": {"id": 42}})
    assert fake.sent == [(42, "Your chat ID is <code>42</code>. Send it to the person who runs this bot.")]
    assert store.active_subscribers(conn) == []                    # asking for the ID doesn't subscribe


def test_active_lists_subscribers_only_for_the_owner(conn, monkeypatch):
    fake = FakeSend()
    monkeypatch.setattr(telegram, "send_message", fake)
    monkeypatch.setenv("OPERATOR_TELEGRAM_CHAT_ID", "1")
    store.subscribe(conn, 1, "Owner", None)
    store.subscribe(conn, 2, "Riya", "riya")
    store.subscribe(conn, 3, "Gone", None)
    store.unsubscribe(conn, 3)

    assert not broadcast.handle_message(conn, {"text": "/active", "chat": {"id": 2}})   # not the owner
    assert fake.sent == []
    assert broadcast.handle_message(conn, {"text": "/active", "chat": {"id": 1}})
    [(chat, text)] = fake.sent
    assert chat == 1 and "· 2 active" in text and "Riya (@riya)" in text and "<code>2</code>" in text
    assert "Gone" not in text and "1 stopped or blocked the bot" in text
