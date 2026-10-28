"""Subscriber bot commands: /search, /city, /filters, /pause, /watch, /applied, /stats, /feedback, with Telegram faked."""

import re

import pytest

from src import broadcast, commands, health, store, telegram
from tests.test_broadcast import conn  # noqa: F401  (the shared fixture: one internship, one 2027 job, in Bengaluru)
from tests.test_match_and_digest import SRC, make_job


class Chat:
    """Records what the bot sends: texts and (name, csv text, caption) files."""

    def __init__(self, monkeypatch):
        self.texts, self.files, self.log = [], [], []
        monkeypatch.setattr(telegram, "send_message", self._text)
        monkeypatch.setattr(telegram, "send_document", lambda chat, name, content, caption="", quiet=False:
                            self.files.append((name, content.decode("utf-8-sig"), caption)) or 1)

    def _text(self, chat, text, buttons=None, quiet=False):
        self.texts.append(text)
        self.log.append((chat, text))
        return 1

    def to(self, chat_id) -> str:
        return "\n".join(t for c, t in self.log if c == chat_id)

    def say(self, db, text, chat_id=42):
        self.texts, self.files, self.log = [], [], []
        return commands.handle_message(db, {"text": text, "chat": {"id": chat_id},
                                              "from": {"first_name": "Riya", "username": "riya"}})


@pytest.fixture
def chat(conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("OPERATOR_TELEGRAM_CHAT_ID", "1")
    c = Chat(monkeypatch)
    store.subscribe(conn, 42, "Riya", "riya")
    return c


def add_jobs(db, *jobs):
    store.save_source_result(db, store.start_run(db), SRC, store.now(), list(jobs))
    db.execute("UPDATE jobs SET backfill = 0, lifecycle = 'open'")
    db.commit()


def test_search_sends_matching_jobs_as_a_file(conn, chat):  # noqa: F811
    assert chat.say(conn, "/search campus")
    [(name, table, caption)] = chat.files
    assert name.startswith("search_") and "2027 Campus Hire" in table and "Engineering Intern" not in table
    assert "campus" in caption and "Jobs for 2027 graduates</b> · 1" in caption and not chat.texts

    chat.say(conn, "/search kubernetes")
    assert not chat.files and "No open internships" in chat.texts[0]
    chat.say(conn, "/search")
    assert "/search react" in chat.texts[0]


def test_city_understands_other_spellings_and_remote(conn, chat):  # noqa: F811
    add_jobs(conn, make_job(20, title="Frontend Intern", location_raw="Remote, India", cities=[], work_mode="remote"))
    chat.say(conn, "/city bangalore")
    [(_, table, caption)] = chat.files
    assert "Bengaluru" in caption and "Software Engineering Intern" in table and "Frontend Intern" not in table
    chat.say(conn, "/city remote")
    [(_, table, _)] = chat.files
    assert "Frontend Intern" in table and "Software Engineering Intern" not in table
    chat.say(conn, "/city atlantis")
    assert "don't know" in chat.texts[0] and not chat.files


def test_filters_narrow_the_daily_list_and_latest(conn, chat):  # noqa: F811
    store.subscribe(conn, 43, "Arun", None)  # no filters: gets everything
    chat.say(conn, "/filters type internships")
    assert "Saved" in chat.texts[0] and "internships only" in chat.texts[0]
    chat.say(conn, "/filters city pune")
    assert "Pune" in chat.texts[0]
    assert store.get_filters(conn, 42) == {"cities": ["Pune"], "type": "internships"}

    chat.log = []
    assert broadcast.send_daily(conn)["sent"] == 2
    assert "Nothing new today for your filters" in chat.to(42)     # their jobs are all in Bengaluru
    assert "Software Engineering Intern" in chat.to(43)

    chat.say(conn, "/filters city any")
    chat.say(conn, "/latest")
    [(_, table, caption)] = chat.files
    assert "internships only" in caption and "Engineering Intern" in table and "Campus Hire" not in table
    chat.say(conn, "/filters reset")
    assert store.get_filters(conn, 42) == {}


def test_paused_subscribers_get_no_daily_list_until_they_resume(conn, chat):  # noqa: F811
    chat.say(conn, "/pause 3")
    assert "No daily lists until" in chat.texts[0]
    chat.texts = []
    r = broadcast.send_daily(conn, once_a_day=False)
    assert (r["sent"], r["paused"]) == (0, 1) and not chat.texts
    chat.say(conn, "/resume")
    assert store.subscriber(conn, 42)["paused_until"] is None


def test_watched_companies_go_on_top_of_the_daily_list(conn, chat):  # noqa: F811
    chat.say(conn, "/watch ACME")
    assert "Watching <b>acme</b>" in chat.texts[0] and "Acme" in chat.texts[0]
    assert store.watches(conn, 42) == ["acme"]
    chat.say(conn, "/watch")
    assert "• acme" in chat.texts[0]
    chat.texts = []
    broadcast.send_daily(conn)
    assert chat.texts[1].startswith("<b>🔔 Companies you watch</b> · 2 new")
    chat.say(conn, "/unwatch acme")
    assert store.watches(conn, 42) == []


def test_watch_warns_about_unknown_companies_and_has_a_limit(conn, chat):  # noqa: F811
    chat.say(conn, "/watch zzzcorp")
    assert "check the spelling" in chat.texts[0]
    for i in range(commands.MAX_WATCHES - 1):
        store.add_watch(conn, 42, f"company {i}")
    chat.say(conn, "/watch one too many")
    assert "up to" in chat.texts[0] and len(store.watches(conn, 42)) == commands.MAX_WATCHES


def test_tapping_a_job_number_marks_it_applied(conn, chat):  # noqa: F811
    broadcast.send_daily(conn)
    daily = "\n".join(chat.texts)
    job_id = int(re.search(r"/a_(\d+)", daily)[1])
    chat.say(conn, f"/a_{job_id}")
    assert "Marked as applied" in chat.texts[0] and f"/u_{job_id}" in chat.texts[0]
    chat.say(conn, f"/a_{job_id}")
    assert "Already marked" in chat.texts[0]
    chat.say(conn, "/applied")
    assert "Jobs you applied to</b> · 1" in chat.texts[0] and f"undo /u_{job_id}" in chat.texts[0]
    chat.say(conn, f"/u_{job_id}")
    assert "Removed" in chat.texts[0]
    chat.say(conn, "/applied")
    assert "haven't marked" in chat.texts[0]
    chat.say(conn, "/a_99999")
    assert "can't find" in chat.texts[0]


def test_csv_has_the_job_number(conn, chat):  # noqa: F811
    chat.say(conn, "/latest")
    [(_, table, _)] = chat.files
    header, first = table.splitlines()[:2]
    assert header.endswith(",id") and first.split(",")[-1].isdigit()


def test_stats_show_owner_numbers_only_to_the_owner(conn, chat):  # noqa: F811
    chat.say(conn, "/stats")
    assert "Software internships: 1" in chat.texts[0] and "Acme (2)" in chat.texts[0]
    assert "Owner only" not in chat.texts[0]
    store.subscribe(conn, 1, "Owner", None)
    chat.say(conn, "/stats", chat_id=1)
    assert "Owner only" in chat.texts[0] and "2 subscribed" in chat.texts[0]


def test_feedback_reaches_the_owner_with_a_daily_limit(conn, chat, monkeypatch):  # noqa: F811
    owner = []
    monkeypatch.setattr(health, "tell_owner", lambda text: owner.append(text) or True)
    chat.say(conn, "/feedback the <b>link</b> is broken")
    assert "Thanks" in chat.texts[0]
    assert "Riya (@riya)" in owner[0] and "&lt;b&gt;link" in owner[0]  # their text can't inject formatting
    for _ in range(commands.MAX_FEEDBACK_PER_DAY):
        chat.say(conn, "/feedback again")
    assert "try again tomorrow" in chat.texts[0] and len(owner) == commands.MAX_FEEDBACK_PER_DAY


def test_settings_need_a_subscription_and_unknown_commands_get_help(conn, chat):  # noqa: F811
    store.unsubscribe(conn, 42)
    for cmd in ("/filters", "/pause", "/watch google"):
        chat.say(conn, cmd)
        assert chat.texts == [commands.NOT_SUBSCRIBED]
    assert not chat.say(conn, "/dance")
    assert "/help" in chat.texts[0]
    assert chat.say(conn, "/search@Bot_Jobfinder_bot campus")  # commands picked from the menu in groups
