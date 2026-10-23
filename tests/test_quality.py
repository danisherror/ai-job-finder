"""Phase 4: keyword baseline, labelling and the AI-vs-baseline comparison, plus remaining safety tests."""

import csv
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from src import baseline, evaluate, llm, main, matcher, store
from src.models import Job, MatchResult, Profile, Source

SRC = Source(name="Acme", type="lever", board="acme")
PROFILE = Profile(id="asha", name="Asha", target_roles=["Backend Engineer"], seniority="mid", experience_years=3,
                  skills_strong=["Python", "PostgreSQL"], skills_some=["Docker"], locations=["Bengaluru"])


def make_job(i: int, title: str = "Backend Engineer", description: str = "Python and PostgreSQL.", **kw) -> Job:
    base = dict(source="Acme", ats_id=f"job{i}", title=title, company="Acme", location_raw="Bengaluru, India",
                cities=["Bengaluru"], work_mode="onsite", india_eligible="yes", description=description,
                apply_url=f"https://jobs.lever.co/acme/job{i}",
                posted_at=datetime.now(timezone.utc) - timedelta(days=i))
    return Job(**(base | kw))


# ---------------------------------------------------------------- keyword baseline

def test_baseline_prefers_role_and_skill_matches():
    jobs = [
        ("sales", make_job(1, "Account Executive", "Sell our product.")),
        ("partial", make_job(2, "Platform Engineer", "Python and Kubernetes.")),
        ("best", make_job(3, "Backend Engineer", "Python, PostgreSQL and Docker.")),
        ("words", make_job(4, "Engineer, Backend Systems", "Python.")),
    ]
    assert baseline.rank(jobs, PROFILE) == ["best", "words", "partial", "sales"]


def test_baseline_ignores_filler_words_in_titles():
    assert baseline.title_score("Senior Engineer", ["Backend Engineer"]) == 0
    assert baseline.title_score("Backend Developer", ["Backend Engineer"]) == 1


# ---------------------------------------------------------------- labelling

@pytest.fixture
def labels(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "LABELS_DIR", tmp_path / "labels")
    monkeypatch.setattr(evaluate, "CACHE_DIR", tmp_path / "cache")
    conn = store.connect(tmp_path / "t.db")
    store.sync_sources(conn, [SRC])
    jobs = [make_job(i) for i in range(1, 21)]
    jobs += [make_job(i, location_raw="Pune", cities=["Pune"]) for i in range(21, 26)]  # fail the location rule
    jobs += [make_job(i, posted_at=datetime(2020, 1, 1, tzinfo=timezone.utc)) for i in range(26, 28)]  # old is fine
    store.save_source_result(conn, store.start_run(conn), SRC, store.now(), jobs)
    return conn


def write_labels(person: str, labels: dict[str, str]) -> None:
    """Fill in the label column: labels maps ats_id → label."""
    rows = evaluate.read_rows(person)
    for r in rows:
        r["label"] = labels.get(r["job_key"].rsplit(":", 1)[-1], r["label"])
    with evaluate.labels_path(person).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=evaluate.COLUMNS)
        w.writeheader()
        w.writerows(rows)


def test_export_picks_only_jobs_that_pass_the_rules(labels):
    added = evaluate.export(labels, PROFILE, count=50, holdout=5)
    rows = evaluate.read_rows("asha")
    assert added == len(rows) == 22  # 20 Bengaluru + 2 old ones; Pune jobs left out
    assert sum(r["set"] == "holdout" for r in rows) == 5
    assert all(r["label"] == "" for r in rows)
    assert set(evaluate.read_jobs("asha")) == {r["job_key"] for r in rows}


def test_export_again_keeps_labels_and_adds_new_jobs(labels):
    evaluate.export(labels, PROFILE, count=10, holdout=4)
    first = evaluate.read_rows("asha")
    write_labels("asha", {first[0]["job_key"].rsplit(":", 1)[-1]: "good"})
    added = evaluate.export(labels, PROFILE, count=50, holdout=4)
    rows = evaluate.read_rows("asha")
    assert added == 12 and len(rows) == 22
    assert rows[0]["label"] == "good"
    assert sum(r["set"] == "holdout" for r in rows) == 4  # holdout already full


def test_unknown_label_is_a_clear_error(labels):
    evaluate.export(labels, PROFILE, count=3, holdout=3)
    write_labels("asha", {r["job_key"].rsplit(":", 1)[-1]: "yes please" for r in evaluate.read_rows("asha")})
    with pytest.raises(ValueError, match="Unknown label"):
        evaluate.labelled("asha", "holdout")


# ---------------------------------------------------------------- comparison

def label_holdout(labels, good: int) -> set[str]:
    """Export 10 holdout jobs and label the last `good` of them good, the rest no. Returns the good ats_ids.

    All test jobs look alike to the keyword baseline, so it keeps file order and its top 5 are all No.
    """
    evaluate.export(labels, PROFILE, count=10, holdout=10)
    ids = [r["job_key"].rsplit(":", 1)[-1] for r in evaluate.read_rows("asha")]
    good_ids = set(ids[len(ids) - good:]) if good else set()
    write_labels("asha", {i: "g" if i in good_ids else "n" for i in ids})
    return good_ids


@pytest.fixture
def ai(monkeypatch):
    """Fake the AI: install(fit_for) rates each job by its ats_id and returns the list of batches scored."""
    calls: list[str] = []

    def install(fit_for):
        def score_batch(profile, resume, jobs):
            calls.append([j.ats_id for j in jobs])
            return {n: MatchResult(fit=fit_for(j.ats_id), reason="r") for n, j in enumerate(jobs, 1)}
        monkeypatch.setattr(matcher, "score_batch", score_batch)
        return calls
    return install


def test_ai_that_finds_the_good_jobs_is_kept(labels, ai):
    good = label_holdout(labels, 4)
    ai(lambda ats_id: "strong" if ats_id in good else "weak")
    r = evaluate.compare(PROFILE)
    assert r["ai"]["good"] == 4 and r["baseline"]["good"] == 0
    assert r["verdict"].startswith("Keep the AI")


def test_ai_that_ranks_badly_is_not_kept(labels, ai):
    good = label_holdout(labels, 3)
    ai(lambda ats_id: "weak" if ats_id in good else "strong")
    r = evaluate.compare(PROFILE)
    assert r["ai"]["good"] == 0
    assert "does not beat" in r["verdict"]


def test_no_labelled_jobs_rated_strong_are_listed(labels, ai, capsys):
    label_holdout(labels, 1)
    ai(lambda ats_id: "strong")
    evaluate.compare(PROFILE)
    assert "Labelled No but rated strong/good" in capsys.readouterr().out


def test_ai_results_are_cached_until_the_prompt_changes(labels, ai, monkeypatch):
    label_holdout(labels, 1)
    calls = ai(lambda ats_id: "good")
    evaluate.compare(PROFILE)
    evaluate.compare(PROFILE)
    assert len(calls) == 1
    monkeypatch.setattr(evaluate.prompts, "RUBRIC_VERSION", "next")
    evaluate.compare(PROFILE)
    assert len(calls) == 2


def test_profile_change_invalidates_the_cache(labels, ai):
    label_holdout(labels, 1)
    calls = ai(lambda ats_id: "good")
    evaluate.compare(PROFILE)
    evaluate.compare(PROFILE.model_copy(update={"skills_strong": ["Go"]}))
    assert len(calls) == 2


def test_training_and_holdout_are_kept_apart(labels, ai):
    evaluate.export(labels, PROFILE, count=10, holdout=4)
    write_labels("asha", {r["job_key"].rsplit(":", 1)[-1]: "m" for r in evaluate.read_rows("asha")})
    ai(lambda ats_id: "good")
    assert evaluate.compare(PROFILE, "holdout")["jobs"] == 4
    assert evaluate.compare(PROFILE, "train")["jobs"] == 6


# ---------------------------------------------------------------- remaining safety tests

def test_second_run_at_the_same_time_exits(tmp_path):
    lock = tmp_path / "run.lock"
    held = main.acquire_lock(lock)  # noqa: F841
    code = f"from pathlib import Path; from src import main; main.acquire_lock(Path({str(lock)!r})); print('ran')"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=main.ROOT)
    assert out.returncode == 0
    assert "Another run is in progress" in out.stdout and "ran" not in out.stdout


def test_bad_ai_output_leaves_jobs_unscored(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    store.sync_sources(conn, [SRC])
    store.save_source_result(conn, store.start_run(conn), SRC, store.now(),
                             [make_job(i, posted_at=datetime.now(timezone.utc)) for i in range(1, 4)])
    store.sync_profiles(conn, [PROFILE])

    def broken(system, prompt):
        raise llm.LLMError("bad_output", "not JSON")
    monkeypatch.setattr(llm, "ask_json", broken)
    r = matcher.match_profile(conn, None, PROFILE)
    assert r["scored"] == 0 and r["unscored"] == 3 and r["errors"] == ["bad_output"]
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
    assert len(store.unscored_jobs(conn, PROFILE)) == 3  # tried again next run


def test_forget_deletes_one_persons_data_only(tmp_path, monkeypatch, capsys):
    conn = store.connect(tmp_path / "t.db")
    store.sync_sources(conn, [SRC])
    store.save_source_result(conn, store.start_run(conn), SRC, store.now(),
                             [make_job(i, posted_at=datetime.now(timezone.utc)) for i in range(1, 3)])
    other = PROFILE.model_copy(update={"id": "ravi"})
    store.sync_profiles(conn, [PROFILE, other])
    monkeypatch.setattr(llm, "ask_json", lambda s, p: {"results": [{"job": 1, "fit": "good", "reason": "r"},
                                                                    {"job": 2, "fit": "weak", "reason": "r"}]})
    matcher.match_profile(conn, None, PROFILE)
    matcher.match_profile(conn, None, other)
    store.set_state(conn, "digest_day:asha", "2026-10-24")
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main.digest, "EXPORTS", tmp_path / "exports")
    monkeypatch.setattr(evaluate, "LABELS_DIR", tmp_path / "labels")
    monkeypatch.setattr(evaluate, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "asha.csv").write_text("x")

    main.forget(conn, "asha")
    people = lambda table: {r[0] for r in conn.execute(f"SELECT profile_id FROM {table}")}  # noqa: E731
    assert people("matches") == people("llm_calls") == people("profiles") == {"ravi"}
    assert store.get_state(conn, "digest_day:asha") is None
    assert not (tmp_path / "exports" / "asha.csv").exists()
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_forget_refuses_while_the_profile_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    (tmp_path / "config" / "profiles").mkdir(parents=True)
    (tmp_path / "config" / "profiles" / "asha.yaml").write_text("name: Asha")
    with pytest.raises(SystemExit, match="first"):
        main.forget(store.connect(tmp_path / "t.db"), "asha")
