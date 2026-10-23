# Open Questions

Answer these before or during Phase 0. Write your answer under each question. Suggested defaults come from the project report.

**Status (2026-10-24):** 10 of 12 decided. **Still open: #5 (the first two people's profiles) and #12 (job volume), both needed before Phase 2.** Privacy to-dos: turn on FileVault; check the Claude "help improve Claude" setting.

## Must answer before building

### 1. Which companies do you want to track?
List the 30–50 target companies. For each, I need to know which job-posting system it uses (Greenhouse, Lever, Ashby, Workday, its own site). I can check these for you if you give me the list.
- **Why it matters:** if many use Workday, SuccessFactors or Darwinbox, the plan needs a Workday adapter in v1.
- **Answer (decided 2026-10-23):**
  - **Main rule:** a job qualifies only if it is located in India, or remote and open to people living in India. Remote jobs restricted to other regions (US-only, EU-only) are dropped. Any company can be added; this rule decides which of its jobs count.
  - **Starter list** (feeds checked live on 2026-10-23; all three groups kept):
    - **A. Indian companies:** Meesho (lever: `meesho`), InMobi (greenhouse: `inmobi`), Sarvam AI (ashby: `sarvam`), Zeta (lever: `zeta`), HackerRank (greenhouse: `hackerrank`), Mindtickle (lever: `mindtickle`), Druva (greenhouse: `druva`), Groww (greenhouse: `groww`), CRED (lever: `cred`), Atlan (ashby: `atlan`)
    - **B. Global with India offices:** Okta, Databricks, MongoDB, Zscaler, Stripe, GitLab, Toast, Elastic, Datadog, Twilio, Coinbase, Airbnb (all greenhouse; board name = lowercase company name)
    - **C. Remote-first:** Grafana Labs (greenhouse: `grafanalabs`), Canonical (greenhouse: `canonical`), Vercel (greenhouse: `vercel`), Supabase, PostHog, Linear, Sentry, Notion (all ashby; board name = lowercase company name)
  - **Not on these feeds (check after v1):** Razorpay, Swiggy, Zomato, PhonePe, Flipkart, Postman, BrowserStack, Freshworks, Zepto, Paytm. Likely Workday or custom career sites. No Workday adapter in v1.
  - **Build notes:** Ashby returns 403 without a `User-Agent` header, so every adapter sends one. Many "remote" jobs at US companies are US-only, so the rule filter must detect region limits (location field + description text).

### 2. Which LLM provider and model?
Claude, GPT, Gemini, or another? And what monthly spending cap?
- **Suggested:** a small, cheap model with JSON output; hard limit around $15/month.
- **Answer (decided 2026-10-23):**
  - **Model:** Claude Haiku.
  - **Access:** Claude Code headless mode (`claude -p --model haiku --output-format json`) called from the script, using the owner's Claude Pro subscription. No API key, no extra cost, no spending cap needed.
  - **Consequences for the build:**
    - All AI calls go through one module (`src/llm.py`) so switching to the paid API later is a one-file change.
    - Batch jobs per person (about 10–20 per call) to stay within Pro usage limits.
    - If a call fails or a limit is hit, jobs stay unscored and are retried next run.
    - Output is still validated with Pydantic; bad JSON counts as a failed call.
    - The machine running the tool must have Claude Code installed and logged in.
  - **Privacy to-do:** check the Claude account's "help improve Claude" setting, since resumes and profiles are sent under a consumer account.

### 3. Where will it run?
Your laptop, an always-on home machine, or a small cloud VM?
- **Why it matters:** a sleeping laptop means no run; a VM costs ~$5/month and puts resumes in the cloud.
- **Answer (decided 2026-10-23):**
  - **Host:** the owner's Mac (macOS), where Claude Code is already installed and logged in.
  - **Scheduler:** `launchd` (a LaunchAgent plist), not cron. A run missed while asleep starts as soon as the Mac wakes, so a digest can be late but never missing.
  - **Wake:** `pmset repeat wakeorpoweron` a few minutes before run time (works when plugged in).
  - **Late-run note:** the digest should show the actual run time, so a late digest is obvious.
  - **Privacy to-do:** FileVault is currently **off**. Turn it on (System Settings → Privacy & Security → FileVault) before adding anyone else's resume.

### 4. Telegram or email for digests?
- **Suggested:** Telegram — free, reaches phones, supports Applied/Skipped buttons. Email from a home machine often lands in spam.
- **Answer (decided 2026-10-23):**
  - **Channel:** Telegram bot only (no email in v1). Each person taps "Start" on the bot once; their chat ID goes in their profile.
  - **Applied / Skipped:** inline buttons under each job in the digest.
  - **Collecting taps:** Telegram keeps updates for only 24 hours, so a separate hourly launchd job polls `getUpdates` and saves decisions (no AI, takes seconds). The daily run also polls first.
  - **Operator messages:** run-failure alerts and the weekly health summary go to the owner's Telegram chat.
  - **CSV export** per person stays as a local file in `private/exports/`.

### 5. Who are the first two people?
You plus one other? I need each person's target roles, skills, years of experience, seniority, locations, salary floor and resume text to write their profile.
- **Answer:** ⏸ **Skipped for now (2026-10-23) — still open.** Needed before Phase 2 (matching). Fields to collect per person: target roles, seniority (junior/mid/senior/lead), years of experience, strong skills, some skills, locations + remote yes/no + work mode, minimum salary (LPA), excluded keywords, jobs per day, resume. Also: who the second person is, and whether the owner targets current level, one level up, or both.

## Can answer during Phase 1–2

### 6. What time should the digest arrive?
- **Suggested:** 7 AM IST.
- **Answer (decided 2026-10-23):** digest arrives **7:00 AM IST**, once a day. launchd starts the run at **6:45 AM**; Mac wakes at **6:40 AM** via `pmset repeat`. If the Mac was asleep, the run starts on wake and the digest shows the actual time.

### 7. First-run backfill rule
- **Suggested:** only jobs posted or updated in the last 14 days; first digest capped at 15.
- **Answer (decided 2026-10-23):**
  - First run: only jobs posted or updated in the **last 7 days** are scored; first digest capped at **10**.
  - Older jobs are saved to the pool but marked as seen. They are never scored or sent.
  - **The same rule applies when a new company is added** (its first scan) and **when a new person is added** (their first digest), so neither causes a flood.
  - Note: Greenhouse exposes `updated_at`, not a true posting date, so "7 days" means "posted or updated in the last 7 days".

### 8. Which 2–3 custom career pages (non-ATS) to start with?
- **Answer (decided 2026-10-23):** **none in v1.** v1 uses only Greenhouse, Lever and Ashby feeds. Scrapling, the AI extractor and the evidence check move to a post-v1 step, together with checking the missing Indian companies (Razorpay, Swiggy, PhonePe, Flipkart, Postman and others) and possibly a Workday adapter. Phase 1 gets shorter; Scrapling doesn't need installing yet. The source config keeps a `type` field so career pages can be added later without changing the matching code.

### 9. Default digest size per person
- **Suggested:** 8 jobs (report says 5–10).
- **Answer (decided 2026-10-23):**
  - **Size:** each person sets their own `digest_size` in their profile (any positive number). If left blank, use **8**.
  - **Order:** strong first, then good; **weak only to fill empty slots**, clearly labelled.
  - **Overflow:** matches that don't fit today are **not** carried to tomorrow. Lists only ever contain new jobs. Overflow still appears in the person's CSV export.

### 10. Python version and package manager
- **Suggested:** Python 3.11+ with `venv` + `pip` and pinned `requirements.txt` (or `uv` if you prefer).
- **Answer (decided 2026-10-23):** **uv + Python 3.12.** `uv` (already installed at `/opt/homebrew/bin/uv`) manages a project-local Python 3.12, dependencies in `pyproject.toml`, exact versions pinned in `uv.lock`. Commands run through `uv run ...`. The system Python (3.9.6) is not used. Claude Code 2.1.308 is installed and is what the launchd job calls.

### 11. Should I make an initial commit?
The repo is initialized but nothing is committed yet. Should I add a `.gitignore` (excluding `private/`, `.env`, `.venv/`) and commit the current docs?
- **Answer (decided 2026-10-23):** yes. Default branch renamed to `main`; `.gitignore` added (excludes `private/`, `.env`, `.venv/`, caches); docs committed locally. No remote.

### 12. Are 30 companies enough? (found in Phase 1, 2026-10-24)
The first real collection found ~5,800 open jobs, of which ~850 are open to people in India. But only **~8 India-eligible jobs are newly posted per day** across all 30 companies (233 in the last 30 days), and that includes sales, HR and other non-tech roles. After each person's filters, that may leave **1–3 new jobs a day per person**. The report assumed ~40.
Top India sources (last 30 days): Okta 36, Zscaler 30, MongoDB 27, InMobi 20, Stripe 15, Supabase 15, Meesho 13, GitLab 12.
- **Options:** (a) add more Greenhouse/Lever/Ashby companies that hire in India (just lines in `sources.yaml`); (b) bring forward the Workday/career-page work for big Indian employers; (c) accept small daily lists.
- **Answer:**
