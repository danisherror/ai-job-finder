"""Label jobs by hand, then check whether the AI ranks them better than plain keyword overlap.

    uv run python -m src.evaluate export <person> [--count 50] [--holdout 15]
        Picks jobs from the pool that pass this person's rules and adds them to tests/labels/<person>.csv
        with an empty `label` column. Fill it in with good, maybe or no (g / m / n also work).
        Running it again adds more jobs; labels already written are kept.

    uv run python -m src.evaluate compare <person> [--train]
        Ranks the labelled holdout jobs (or the training jobs with --train) with the keyword baseline and
        with the AI, and shows how many Good jobs each puts in its top 5.
        Tune prompts on --train; look at the holdout only to decide.

AI results are cached per rubric version and profile in private/eval_cache/, so re-running costs nothing
until the prompt or the profile changes.
"""

import csv
import hashlib
import json
import random
import sys
from datetime import timezone
from pathlib import Path

from src import baseline, llm, matcher, prompts, store
from src.config import ROOT, load_profiles, read_resume
from src.models import Job, MatchResult, Profile
from src.rules import passes_rules

LABELS_DIR = ROOT / "tests" / "labels"
CACHE_DIR = ROOT / "private" / "eval_cache"
COLUMNS = ["job_key", "set", "label", "title", "company", "location", "apply_url"]
LABELS = {"good": "good", "g": "good", "maybe": "maybe", "m": "maybe", "no": "no", "n": "no"}
FIT_ORDER = {"strong": 0, "good": 1, "weak": 2, "no": 3}
TOP = 5


# ---------------------------------------------------------------- label files

def labels_path(person: str) -> Path:
    return LABELS_DIR / f"{person}.csv"


def jobs_path(person: str) -> Path:
    """Full copies of the labelled jobs, so results don't change when the pool does."""
    return LABELS_DIR / f"{person}.jobs.jsonl"


def read_rows(person: str) -> list[dict]:
    path = labels_path(person)
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def read_jobs(person: str) -> dict[str, Job]:
    path = jobs_path(person)
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            item = json.loads(line)
            out[item["job_key"]] = Job(**item["job"])
    return out


def candidates(conn, profile: Profile) -> list[tuple[str, Job]]:
    """Open jobs in the pool that pass this person's rules. Posting age is ignored: old jobs label fine."""
    ageless = profile.model_copy(update={"max_age_days": 10**6})
    rows = conn.execute("SELECT * FROM jobs WHERE lifecycle = 'open' ORDER BY job_key").fetchall()
    out = []
    for row in rows:
        job = store.job_from_row(row)
        if passes_rules(job, ageless)[0]:
            out.append((row["job_key"], job))
    return out


def export(conn, profile: Profile, count: int = 50, holdout: int = 15, seed: int | None = None) -> int:
    """Add up to `count` new jobs to the person's label file. Returns how many were added."""
    rows = read_rows(profile.id)
    jobs = read_jobs(profile.id)
    have = {r["job_key"] for r in rows}
    pool = [(k, j) for k, j in candidates(conn, profile) if k not in have]
    rng = random.Random(seed if seed is not None else profile.id)
    picked = rng.sample(pool, min(count, len(pool)))

    holdout_left = max(0, holdout - sum(r["set"] == "holdout" for r in rows))
    for i, (key, job) in enumerate(picked):
        rows.append({"job_key": key, "set": "holdout" if i < holdout_left else "train", "label": "",
                     "title": job.title, "company": job.company, "location": job.location_raw,
                     "apply_url": str(job.apply_url)})
        jobs[key] = job

    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    with labels_path(profile.id).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in COLUMNS} for r in rows)
    jobs_path(profile.id).write_text(
        "".join(json.dumps({"job_key": k, "job": j.model_dump(mode="json")}) + "\n" for k, j in jobs.items()))
    return len(picked)


def labelled(person: str, which: str) -> list[tuple[str, Job, str]]:
    """(job_key, job, label) for labelled rows in the given set. Unknown labels stop with a clear error."""
    jobs = read_jobs(person)
    out = []
    for r in read_rows(person):
        raw = r["label"].strip().lower()
        if r["set"] != which or not raw:
            continue
        if raw not in LABELS:
            raise ValueError(f"Unknown label {r['label']!r} for {r['title']} (use good, maybe or no)")
        if r["job_key"] not in jobs:
            raise ValueError(f"No saved job text for {r['job_key']}; run export again")
        out.append((r["job_key"], jobs[r["job_key"]], LABELS[raw]))
    return out


# ---------------------------------------------------------------- AI scores

def cache_key(profile: Profile, resume: str) -> str:
    digest = hashlib.sha256((profile.model_dump_json() + resume).encode()).hexdigest()[:12]
    return f"{prompts.RUBRIC_VERSION}|{digest}"


def ai_results(profile: Profile, jobs: list[tuple[str, Job]]) -> dict[str, MatchResult]:
    """The AI's verdict for each job, from the cache when the prompt and profile haven't changed.

    Raises llm.LLMError if the AI can't be reached; jobs it skipped are simply missing from the result.
    """
    resume = read_resume(profile)
    key = cache_key(profile, resume)
    path = CACHE_DIR / f"{profile.id}.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    known = cache.get(key, {})

    todo = [(k, j) for k, j in jobs if k not in known]
    for i in range(0, len(todo), matcher.BATCH_SIZE):
        batch = todo[i:i + matcher.BATCH_SIZE]
        results = matcher.score_batch(profile, resume, [j for _, j in batch])
        for number, result in results.items():
            known[batch[number - 1][0]] = result.model_dump()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: known}, indent=1))  # older versions are dropped
    return {k: MatchResult(**known[k]) for k, _ in jobs if k in known}


def _posted(job: Job) -> float:
    when = job.posted_at or job.updated_at
    if when is None:
        return 0.0
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def ai_rank(jobs: list[tuple[str, Job]], results: dict[str, MatchResult]) -> list[str]:
    """Job keys in the order the daily list would use: fit, then newest first. Unscored jobs go last."""
    def order(item):
        key, job = item
        fit = results[key].fit if key in results else None
        return (FIT_ORDER.get(fit, 9), -_posted(job))
    return [key for key, _ in sorted(jobs, key=order)]


# ---------------------------------------------------------------- comparison

def top_counts(order: list[str], labels: dict[str, str], n: int = TOP) -> dict[str, int]:
    top = [labels[k] for k in order[:n]]
    return {"good": top.count("good"), "maybe": top.count("maybe"), "no": top.count("no")}


def verdict(ai: dict[str, int], base: dict[str, int]) -> str:
    """Keep the AI only if its top 5 has at least 3 Good jobs and more than the baseline's."""
    if ai["good"] >= 3 and ai["good"] > base["good"]:
        return "Keep the AI: more Good jobs in its top 5 than the keyword baseline."
    if ai["good"] > base["good"]:
        return "The AI beats the baseline, but has fewer than 3 Good jobs in its top 5. Tune the prompt or rules."
    return "The AI does not beat the keyword baseline. Tune the prompt on --train, or rank by keywords instead."


def compare(profile: Profile, which: str = "holdout") -> dict:
    rows = labelled(profile.id, which)
    if not rows:
        raise ValueError(f"No labelled {which} jobs in {labels_path(profile.id)}; run export and fill in `label`")
    jobs = [(k, j) for k, j, _ in rows]
    labels = {k: lab for k, _, lab in rows}
    titles = {k: f"{j.title} · {j.company}" for k, j in jobs}

    results = ai_results(profile, jobs)
    base_order = baseline.rank(jobs, profile)
    ai_order = ai_rank(jobs, results)
    base, ai = top_counts(base_order, labels), top_counts(ai_order, labels)

    print(f"{profile.id}: {len(rows)} labelled {which} jobs "
          f"({sum(v == 'good' for v in labels.values())} good, {sum(v == 'maybe' for v in labels.values())} maybe, "
          f"{sum(v == 'no' for v in labels.values())} no); AI scored {len(results)}")
    if len(rows) < 2 * TOP:
        print(f"  Few jobs: with under {2 * TOP} the top {TOP} says little.")
    for name, order in (("Keyword baseline", base_order), ("AI", ai_order)):
        counts = top_counts(order, labels)
        print(f"\n{name} top {TOP}: {counts['good']} good, {counts['maybe']} maybe, {counts['no']} no")
        for k in order[:TOP]:
            fit = f"[{results[k].fit}] " if name == "AI" and k in results else ""
            print(f"  {labels[k]:<5} {fit}{titles[k]}")

    # Jobs the person said No to but the AI would send near the top: the most useful ones to read when tuning
    misses = [k for k in ai_order if labels[k] == "no" and k in results and results[k].fit in ("strong", "good")]
    if misses:
        print("\nLabelled No but rated strong/good by the AI:")
        for k in misses:
            print(f"  [{results[k].fit}] {titles[k]}: {results[k].reason}")

    text = verdict(ai, base)
    print("\n" + text)
    return {"ai": ai, "baseline": base, "verdict": text, "scored": len(results), "jobs": len(rows)}


def _flag(argv: list[str], name: str, default: int) -> int:
    return int(argv[argv.index(name) + 1]) if name in argv else default


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[1] not in ("export", "compare"):
        sys.exit(__doc__)
    command, person = argv[1], argv[2]
    profile = next((p for p in load_profiles() if p.id == person), None)
    if profile is None:
        sys.exit(f"No profile config/profiles/{person}.yaml")
    if command == "export":
        added = export(store.connect(), profile, _flag(argv, "--count", 50), _flag(argv, "--holdout", 15))
        print(f"Added {added} jobs to {labels_path(person)}. Fill in the label column with good, maybe or no.")
        return
    try:
        compare(profile, "train" if "--train" in argv else "holdout")
    except llm.LLMError as e:
        sys.exit(f"The AI could not be reached ({e.reason}): {e}")
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main(sys.argv)
