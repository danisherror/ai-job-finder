"""Per person: rule filter, then the AI in batches. Each job is scored once per person."""

import time
from collections import Counter

from pydantic import ValidationError

from src import llm, prompts, rules, store
from src.config import read_resume
from src.models import Job, MatchResult, Profile

BATCH_SIZE = 15
MAX_JOBS_PER_RUN = 150  # per person; protects the Pro plan's usage limit. The rest wait for the next run.
STOP_REASONS = {"usage_limit", "logged_out", "not_installed"}  # no point trying more batches today


def parse_results(data: dict, count: int) -> dict[int, MatchResult]:
    """Valid results keyed by job number (1..count). Anything malformed, unknown or repeated is dropped."""
    out: dict[int, MatchResult] = {}
    items = data.get("results")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        number = item.get("job")
        if not isinstance(number, int) or not 1 <= number <= count or number in out:
            continue
        try:
            out[number] = MatchResult(**{k: v for k, v in item.items() if k != "job"})
        except ValidationError:
            continue
    return out


def score_batch(profile: Profile, resume: str, jobs: list[Job]) -> dict[int, MatchResult]:
    data = llm.ask_json(prompts.SYSTEM, prompts.build_prompt(profile, resume, jobs))
    return parse_results(data, len(jobs))


def match_profile(conn, run_id: str | None, profile: Profile) -> dict:
    """Score this person's new jobs. Returns counts for the run log; raises nothing for AI failures."""
    candidates = store.unscored_jobs(conn, profile)
    passed: list[tuple[str, Job]] = []
    dropped: Counter = Counter()
    for key, job in candidates:
        ok, reason = rules.passes_rules(job, profile)
        if ok:
            passed.append((key, job))
        else:
            dropped[reason.split(" '")[0]] += 1

    resume = read_resume(profile)
    todo = passed[:MAX_JOBS_PER_RUN]
    scored, errors, stopped = 0, [], None
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        started = time.monotonic()
        try:
            results = score_batch(profile, resume, [job for _, job in batch])
        except llm.LLMError as e:
            store.record_llm_call(conn, run_id, profile.id, len(batch), 0, time.monotonic() - started, e.reason)
            errors.append(e.reason)
            if e.reason in STOP_REASONS:
                stopped = e.reason
                break
            continue
        for number, result in results.items():
            store.save_match(conn, run_id, profile.id, batch[number - 1][0], result, prompts.RUBRIC_VERSION)
        scored += len(results)
        store.record_llm_call(conn, run_id, profile.id, len(batch), len(results), time.monotonic() - started, None)

    return {
        "candidates": len(candidates),
        "passed": len(passed),
        "dropped": dict(dropped.most_common()),
        "scored": scored,
        "unscored": len(passed) - scored,
        "errors": errors,
        "stopped": stopped,
    }
