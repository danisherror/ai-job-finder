# AI Job Finder

Collects tech jobs every morning from company job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
Workable, Oracle Cloud HCM, Eightfold, Rippling, Jibe), Amazon's careers site and nine job boards (Himalayas,
We Work Remotely, YC startup jobs, Remote OK, Remotive, Working Nomads, Jobspresso, Hasjob, Hacker News
"Who is hiring?"), keeps the ones open to people in India, and sends each person a short ranked list on Telegram by 7:00 AM IST.

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
   - Jibe (iCIMS): the career site's base URL, e.g. `board: "https://careers.amd.com"`
   - Amazon: `type: amazon, board: IND` (a country code; only tech job categories are kept)
   - RippleHire: the career site link with its token, plus `#list/geo=India` where the site offers that filter, e.g.
     `board: "https://ltimindtree.ripplehire.com/candidate/?token=xviyQvbnyYZdGtozXoNm#list/geo=India"`
   - Phenom: the career site URL with its locale path, e.g. `board: "https://careers.nttdata.com/global/en"`
   - Avature: the portal URL plus the search words, e.g.
     `board: "https://jobs.siemens.com/en_US/externaljobs?search=India"`
   - TalentBrew (Radancy), iCIMS classic: the career site's base URL, e.g. `board: "https://careers.arm.com"`
     or `board: "https://ideasglobal-sas.icims.com"`. These are read through the site's sitemap and job pages.
2. Add a line to `config/sources.yaml`: `- {name: Acme, type: lever, board: acme}`
3. Check it: `uv run python -m src.main collect` should show `ok  Acme  N jobs`.

On its first scan, jobs older than 7 days are stored as already seen, so nobody gets a flood of old jobs.
To pause a company, add `active: false` to its line.

SmartRecruiters, Workday, Oracle, Eightfold, Rippling, RippleHire, Phenom, Avature, TalentBrew and iCIMS sources
open each new job's page once for its description, so a big company's first scan takes a few minutes; later scans
only open new jobs. Avature and the sitemap readers (TalentBrew, iCIMS, Societe Generale) open at most 150-200 new
jobs per scan, newest first where the site allows; the rest follow on the next scans.

Sites left out on purpose: LinkedIn and Indeed jobs, Naukri and Hirist (their robots.txt forbids it),
Foundit, Internshala and Instahyre (block automated access), Wellfound and Cutshort (no public API),
Google Careers (web page only, no API) and Apple (its search API returned nothing for India).
Remote job boards Remote OK, Remotive, Working Nomads, Jobspresso and Jobicy were added on 2026-10-24 even though
few of their jobs are open to India (about 15 a day between them, plus ~40 of Jobicy's newest 100); each costs one
call per scan.
Python.org jobs (no India jobs) and Wipro's job feed (ignores search and paging) were checked and left out.
YC jobs need a free YC account to apply.
From the third shared company list (2026-10-24), readers were later written for Atlassian, Arm, IDeaS, Optum
(UnitedHealth Group), Societe Generale, Synopsys, Siemens, Cencora, NTT DATA, LTIMindtree, Mphasis and Tata
Technologies. Still not added: SAP SuccessFactors sites (SAP, Cargill, Globant, HCLTech, Wipro), because their only
readable job feed is under a path their robots.txt disallows (and SAP's site shows a bot check), and in-house sites
(Google DeepMind, Goldman Sachs, IBM, Intuit, Uber, Walmart, TCS and most Indian startups such as PhonePe, Zomato,
Zepto, Myntra and MakeMyTrip). The full list is in `config/unsupported_sources.yaml`.
Thoughtworks, Pinterest, Riot Games, Netcracker, Hugging Face and Circle have public boards with no India jobs.

From the list of 120 job portals (2026-10-24), Hasjob and Hacker News "Who is hiring?" were added as job boards,
and HackerOne, Bugcrowd and Built In as companies (their own jobs). Already covered: Himalayas, We Work Remotely,
Remote OK, Remotive, Working Nomads, Jobspresso, YC jobs and Turing. The rest were left out:
- Blocked automated access when checked (HTTP 403, a bot check or no answer): WorkIndia, Job Hai, JobGrin,
  Startup Jobs, FlexJobs, Remote.co, Golang Cafe, Rust Jobs, Ruby on Remote, StepStone, CV-Library, Totaljobs,
  CareerOne, GrabJobs, JobisJob, Naukrigulf, ClearanceJobs and the Linux Foundation job board.
- Official API that needs a key: Jooble, Careerjet and Adzuna have readers (keys go in `.env`; Adzuna stays off
  in `sources.yaml` until its app ID and key are there). Talent.com's API is only for approved publishers and has
  no public documentation, so it has no reader.
- Search pages only, no public feed or API: TimesJobs, Shine, iimjobs (and its executive search), Glassdoor,
  Apna, Freshersworld, Placement India, AmbitionBox, Fresherslive, Unstop, Firstnaukri, eJobIndia, Google Jobs,
  SimplyHired, ZipRecruiter, Monster (and Monster Gulf), CareerBuilder, Dice, TechFetch, Otta / Welcome to the
  Jungle, Hired, The Muse, JustRemote, Dynamite Jobs, JS Remotely, Flexa, Relocate.me, F6S, Jobrapido, Jobted,
  JobStreet (and JobStreet Express), JobsDB, Jora, SEEK, Reed, No Fluff Jobs, Foundit Gulf, GulfTalent and Bayt.
- Need a candidate login: Superset (campus placements); Toptal, Arc, Contra, Gun.io and Lemon.io are freelance
  marketplaces, and their own hiring boards had no India jobs.
- Wrong region: Landing.jobs (almost all Portugal), EURES (EU), USAJOBS and GovernmentJobs (US government).
- Government and apprenticeship portals (National Career Service, Employment News, NATS, Apprenticeship India,
  AICTE internships, Public Service Commission sites): no feed, mostly notices or PDFs.
- No job feed: CNCF (its feed was empty), OpenSSF and OWASP.
- Staffing firms (Michael Page, Robert Walters, Randstad, Adecco, TeamLease, Quess, CIEL HR, ABC Consultants,
  Kelly, ManpowerGroup, SutraHR, Flexing It): their own sites, not on a job system this tool reads.

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
