# AI Job Finder — Implementation Plan

Based on `AI_Job_Finder_Project_Report.md`, updated with the decisions in `question.md` (2026-10-23). Where they differ, `question.md` wins.

About 3–4 weeks at 2–3 hours a day: a usable daily list after week 2, reliable enough to leave running after week 4.

**Start small:** 2 people, the ~30 confirmed starter companies. Add more only after two clean weeks.

---

## Decisions (summary of `question.md`)

| Topic | Decision |
| --- | --- |
| Which jobs count | Located in India, or remote **and open to people in India**. Remote jobs limited to other regions are dropped |
| Sources (v1) | Greenhouse, Lever, Ashby feeds only. ~30 confirmed companies (see `question.md` #1) |
| Career pages / Scrapling | **Not in v1.** Added after v1 together with Razorpay, Swiggy, PhonePe and others, and maybe a Workday adapter |
| AI | Claude Haiku via **Claude Code headless mode** (`claude -p`) on the owner's Pro plan. All calls in `src/llm.py`; jobs batched 10–20 per call |
| Host | The owner's Mac. `launchd` schedule (missed runs start on wake) + `pmset` scheduled wake |
| Run time | Wake 6:40, run 6:45, list by **7:00 AM IST** |
| Delivery | **Telegram only**, Applied/Skipped buttons; hourly tap-collector job. Alerts + weekly health to the owner's Telegram |
| First run | Only jobs posted/updated in the last **7 days**; first list max **10**. Same rule for each new company and new person |
| List size | Per person (`digest_size`), default 8. Strong → good → weak only to fill. No carry-over |
| Python | **uv + Python 3.12**, `pyproject.toml` + `uv.lock` |
| Still open | The first two people's profiles (#5), needed before Phase 2 |

---

## Phase 0 — Setup (1 day)

- [x] `git init` (local only, branch `main`); `.gitignore` with `private/`, `.env`, `.venv/`, caches
- [ ] **(Owner)** Turn on FileVault; check the Claude "help improve Claude" setting
- [x] `uv init` with Python 3.12; add `httpx`, `pydantic`, `pyyaml`, `pytest` (pinned via `uv.lock`)
- [x] `.env.example` added
- [ ] **(Owner)** Create the Telegram bot with @BotFather; copy `.env.example` to `.env`; fill in the token and your chat ID
- [x] Confirm `claude -p --model haiku --output-format json` works from a script (~4 s per call; no API key set, so it uses the Pro plan; each call carries ~13k tokens of Claude Code's own setup, so batching matters)
- [x] Create the folder layout and `config/sources.yaml` (30 sources: 19 Greenhouse, 4 Lever, 7 Ashby)
- [x] Profile template `config/profiles/example.yaml`; real profiles are git-ignored
- Note: Groww's Greenhouse board currently lists only non-tech roles; keep it and watch whether it ever produces matches

**Done when:** `uv run python -m src.collect.greenhouse groww` prints Groww's jobs.

---

## Phase 1 — Collect into the job pool (3–4 days)

Storage and models first, so every adapter writes the same shape.

1. **`src/models.py`** — Pydantic models: `Source`, `Job`, `Profile`, `MatchResult`.
2. **`src/store.py`** — SQLite (`private/jobs.db`):
   - Tables `sources`, `jobs`, `runs`
   - `job_key()`: ATS job ID → canonical apply URL → hash(company + title + location)
   - `upsert_job()`: insert new, or only update `last_seen` (idempotent)
   - `save_source_result(run_id, source, jobs)`: jobs + "source done" in **one transaction**
   - First scan of a source: jobs older than 7 days are stored as already seen
3. **`src/normalize.py`**:
   - Location alias table (Bangalore → Bengaluru, Gurgaon → Gurugram, …) and work mode (remote/hybrid/onsite)
   - **India eligibility:** India location → yes; remote → check location + description for region limits ("US only", "must reside in", "EMEA", time-zone requirements); unclear → keep, and let the matcher flag it
   - Experience regex (`3+ years`, `2-5 years`)
   - Tested against real feed data from the starter companies
4. **ATS adapters** (`src/collect/greenhouse.py`, `lever.py`, `ashby.py`) — each has `fetch(source) -> list[Job]`. `httpx` with a `User-Agent` header (Ashby needs one), timeouts, retry with backoff. Greenhouse fetches with `?content=true` for descriptions.
5. **`src/config.py`** — load and validate `config/sources.yaml` and `config/profiles/*.yaml`.
6. **`src/main.py` (collect stage)** — each source in its own try/except; record `ok` / `failed` + reason in `runs`.

**Done when:** a run fills the pool from all starter sources, and a second run adds zero duplicates.

**Status (2026-10-24): done.** `uv run python -m src.main collect` scans all 30 sources (~5,800 jobs) in about a minute; a second run adds no duplicates; 44 offline tests pass (`uv run pytest`).
- Built: `models.py`, `collect/http.py` (retries, `User-Agent`, failure reasons), the three adapters, `normalize.py`, `store.py`, `config.py`, `main.py` (with the run lock, brought forward from Phase 2).
- Run records are split into `runs` (one row per run) and `source_runs` (one row per source per run).
- The 7-day rule uses the **posting date**, not `updated_at`: Stripe and MongoDB bump `updated_at` on every job daily.
- Real data: ~850 open jobs are India-eligible (yes/unclear), but only **~8 per day** are newly posted across all 30 companies, non-tech included. See `question.md` #12.
- `private/jobs.db` is not filled yet. Testing used a scratch database, so tracking starts on the first real run.

---

## Phase 2 — Match and deliver per person (4–5 days)

*Needs `question.md` #5 answered first.*

1. **Profiles** — `config/profiles/<person>.yaml` for the owner and one more person; resumes in `private/resumes/`. Fields include `telegram_chat_id`, `seniority`, `digest_size`.
2. **`src/rules.py`** — `passes_rules(job, profile) -> (bool, reason)`: India eligibility, location, work mode, experience range, required/excluded keywords, posting age, seniority. Pure functions, unit tested.
3. **`src/prompts.py`** — matching prompt: fit levels (strong / good / weak), weighted rubric, job text marked as untrusted data; `RUBRIC_VERSION` constant.
4. **`src/llm.py`** — the only place that calls the AI: runs `claude -p --model haiku --output-format json` via `subprocess` with a timeout, parses and validates the JSON. Swappable for the API later.
5. **`src/matcher.py`** — per person, takes unscored jobs that passed the rules, in batches of 10–20 → `MatchResult` each, stored with `rubric_version`. On failure or usage limit, jobs stay unscored for the next run. Logs calls and duration per run.
6. **`matches` table** — `delivery` (pending/sent/failed) and `decision` (none/applied/skipped) kept separate.
7. **`src/telegram.py`** — send messages with inline Applied/Skipped buttons; `poll_updates()` saves button taps as decisions.
8. **`src/digest.py`** — per person: up to `digest_size` new matches (strong → good → weak to fill), never closed or already-sent jobs. Each item: title, company, location, fit, matched/missing skills, apply link. Footer: "Scanned X of Y sources · run at HH:MM". Empty day: "No new matches today". Overflow goes to `private/exports/<person>.csv`. Mark `sent` only after Telegram confirms.
9. **Run lock + schedule** — lock file in `main.py`; `run_id` per run; LaunchAgent plists for the daily run (6:45) and the hourly tap collector; `pmset repeat wakeorpoweron` at 6:40.

**Done when:** two people get their own correct list every morning for 3 days in a row.

---

## Phase 3 — Reliability (4–5 days)

- [ ] **Resumable runs:** a restart with an unfinished `run_id` skips sources already marked done
- [ ] **Job lifecycle:** each *successful* scan without the job → `possibly_closed`, then `closed` after 2. Failed scans never change lifecycle
- [ ] **Suspicious zero:** track usual job count per source; a sudden 0 is marked `suspicious` and closes nothing
- [ ] **Alerts:** whole-run failure, or `claude -p` unavailable/logged out → immediate Telegram message to the owner
- [ ] **`src/health.py`:** weekly Telegram summary to the owner: success rate per source, suspicious zeros, failures with reasons, unscored backlog
- [ ] **Retention:** delete run logs and cached AI requests older than 30 days

**Done when:** it runs unattended for a week, and a deliberately broken source appears in the health summary instead of vanishing.

---

## Phase 4 — Quality and more people (3–4 days)

- [ ] **Labels:** 30–50 jobs per person as Good / Maybe / No in `tests/labels/`; ~15 kept aside as a holdout set
- [ ] **Baseline comparison:** keyword-overlap ranker vs AI matcher; keep AI only if its top 5 has clearly more Good jobs on the holdout set
- [ ] Tune prompts and rules (bump `RUBRIC_VERSION` on every change)
- [ ] **Tests** (offline, `tests/fixtures/` with saved real feed responses):
  - Parsing for each ATS; job identity (same job twice → one row)
  - India eligibility: India / remote-global / remote-US-only examples
  - Hostile job description: hidden injection text doesn't change the match; off-domain apply link rejected
  - Crash mid-run → restart finishes the remaining sources, no duplicate lists
  - Two runs at once → the second exits
  - Dead source → marked failed, closes no jobs; suspicious zero flagged
  - `llm.py` with bad JSON / timeout → jobs stay unscored
- [ ] Add remaining people and companies
- [ ] **README:** add a person or company in under 5 minutes; how to run; where data lives; how to delete someone's data

**Done when:** most of each person's top 5 are jobs they'd actually apply to.

---

## After v1

1. Check which system Razorpay, Swiggy, Zomato, PhonePe, Flipkart, Postman, BrowserStack, Freshworks, Zepto and Paytm use; add a Workday adapter and/or Scrapling career pages with AI extraction + evidence check.
2. Use Applied / Skipped history to adjust each person's ranking.
3. A private, logged-in web page per person.

---

## Project structure

```
AI Job Finder/
├── config/
│   ├── sources.yaml
│   └── profiles/*.yaml
├── private/                 # never committed
│   ├── resumes/
│   ├── exports/
│   └── jobs.db
├── launchd/                 # LaunchAgent plists (daily run, hourly taps)
├── src/
│   ├── collect/{greenhouse,lever,ashby}.py
│   ├── config.py  normalize.py  store.py   models.py
│   ├── rules.py   matcher.py    prompts.py llm.py
│   ├── telegram.py digest.py    health.py  main.py
├── tests/{fixtures,labels}/
├── pyproject.toml  uv.lock  .env  .env.example  README.md
```

---

## Rules to keep throughout

- **A failure must show up as a failure, never as "no new jobs."**
- Hard requirements are checked in code; the AI never decides anything code can check.
- Job text is untrusted data: validate output, check apply-link domains, never follow instructions found in it.
- Each job is scored once per person.
- Never auto-apply; never scrape LinkedIn/Indeed; keep request rates low.
- Everyone's data stays in `private/` on this Mac; each person sees only their own list.

## Success targets

| Metric | Target |
| --- | --- |
| Source success rate | 95%+ |
| Unreported failures | 0 |
| Duplicate or closed jobs sent | 0 |
| Good jobs in AI top 5 (holdout) | 3+ and above keyword baseline |
| Your maintenance time | 1–2 hours/week |
