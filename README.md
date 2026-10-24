# AI Job Finder

Collects tech jobs every morning from company job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
Workable, Oracle Cloud HCM, Eightfold, Rippling), Amazon's careers site and three job boards (Himalayas, We Work Remotely, YC startup jobs), keeps the ones open to people in India, and sends each person a short ranked list on Telegram by 7:00 AM IST.

Design: `AI_Job_Finder_Project_Report.md`. Build plan and status: `PLAN.md`.

## Setup (once)

```sh
uv sync                       # Python 3.12 and the dependencies
cp .env.example .env          # then fill in the Telegram bot token and your own chat ID
claude                        # log in to Claude Code once; matching runs `claude -p` on your plan
```

Install the daily schedule with the steps in `launchd/README.md`.

## Run

```sh
uv run python -m src.main daily              # everything: collect, match, send (what the 6:45 job runs)
uv run python -m src.main collect            # only fetch jobs (no AI); also refreshes all_jobs.csv
uv run python -m src.main export             # rewrite private/exports/all_jobs.csv from the stored jobs
uv run python -m src.main match              # only score new jobs for each person
uv run python -m src.main digest --dry-run   # print each person's list without sending it
uv run python -m src.main health             # the weekly health summary (add --send to send it to you)
uv run pytest -q                             # offline tests
```

Logs from the scheduled runs are in `private/logs/`.

## Browse every job yourself (no AI)

`collect` writes `private/exports/all_jobs.csv`: every job found so far, newest first, with company, title,
location, work mode, India eligibility, experience range, salary (when listed), status (open, possibly_closed,
closed) and apply link. Open it in Excel, Numbers or Google Sheets and filter it yourself.
No Claude login or Telegram setup is needed for this.

## Add a person (about 5 minutes)

1. Ask them to open the bot in Telegram and send `/start`. Within the hour the bot replies with their chat ID
   (or run `uv run python -m src.main taps` to get it now).
2. Copy the template: `cp config/profiles/example.yaml config/profiles/<name>.yaml`. The file name is their ID.
3. Fill in `name`, `telegram_chat_id`, `target_roles`, `seniority`, `experience_years`, skills and `locations`.
   The comments in the template explain each field.
4. Optional: save their resume as plain text in `private/resumes/<name>.txt` and set `resume_file`.
5. Check it: `uv run python -m src.main match` then `uv run python -m src.main digest --dry-run`.

Their first list covers only jobs from the last 7 days and has at most 10 jobs.

## Add a company (about 2 minutes)

1. Find its job board. The board name is the last part of the URL:
   - Greenhouse: `boards.greenhouse.io/<board>` or `job-boards.greenhouse.io/<board>`
   - Lever: `jobs.lever.co/<board>`
   - Ashby: `jobs.ashbyhq.com/<board>`
   - SmartRecruiters: `jobs.smartrecruiters.com/<board>` (the board name is case-sensitive)
   - Workday: use the whole career site URL as the board, e.g.
     `board: "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"`
   - Workable: `apply.workable.com/<board>`
   - Rippling: `ats.rippling.com/<board>/jobs`
   - Oracle Cloud HCM: use the whole career site URL, e.g.
     `board: "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"`
   - Eightfold: the career site URL plus the company's domain, e.g.
     `board: "https://qualcomm.eightfold.ai/careers?domain=qualcomm.com"`
   - Amazon: `type: amazon, board: IND` (a country code; only tech job categories are kept)
2. Add a line to `config/sources.yaml`: `- {name: Acme, type: lever, board: acme}`
3. Check it: `uv run python -m src.main collect` should show `ok  Acme  N jobs`.

On its first scan, jobs older than 7 days are stored as already seen, so nobody gets a flood of old jobs.
To pause a company, add `active: false` to its line.

SmartRecruiters, Workday, Oracle, Eightfold and Rippling sources collect only India jobs and open each new
job's page once for its description, so a big company's first scan takes a few minutes; later scans only open new jobs.

Sites left out on purpose: LinkedIn and Indeed jobs, Naukri and Hirist (their robots.txt forbids it),
Foundit, Internshala and Instahyre (block automated access), Wellfound and Cutshort (no public API),
Google Careers (web page only, no API) and Apple (its search API returned nothing for India).
Remote job boards Remotive, RemoteOK, Working Nomads and Jobicy were checked on 2026-10-24 and had almost
no jobs open to India. Hacker News "Who is hiring" was skipped (about 8 India posts a month, free text).
YC jobs need a free YC account to apply.

## Where data lives

Everything personal stays on this Mac in folders git ignores:

| What | Where |
| --- | --- |
| Profiles | `config/profiles/<name>.yaml` (only `example.yaml` is committed) |
| Resumes | `private/resumes/` |
| Jobs, matches, run history | `private/jobs.db` (SQLite) |
| Full list of each person's matches | `private/exports/<name>.csv` |
| Every collected job | `private/exports/all_jobs.csv` |
| Logs | `private/logs/` |
| Match-quality labels | `tests/labels/<name>.csv` and `.jobs.jsonl` |
| Telegram token, your chat ID | `.env` |

Run logs and AI call logs older than 30 days are deleted automatically. Jobs are kept.

## Delete someone's data

```sh
rm config/profiles/<name>.yaml private/resumes/<name>.*
uv run python -m src.main forget <name>
```

`forget` deletes their matches, AI call log, list export, labels and cached evaluation results. It refuses to run
while their profile still exists, because the next run would add them back.

## Check match quality

Each person labels 30–50 jobs; the AI has to rank them better than simple keyword overlap.

```sh
uv run python -m src.evaluate export <name>          # adds 50 jobs to tests/labels/<name>.csv (15 kept aside)
# fill in the `label` column: good, maybe or no
uv run python -m src.evaluate compare <name> --train  # use while changing prompts or rules
uv run python -m src.evaluate compare <name>          # the kept-aside jobs: use only to decide
```

When you change the prompt in `src/prompts.py`, bump `RUBRIC_VERSION`. AI results are cached per version, so a
comparison only calls the AI again after a change.
