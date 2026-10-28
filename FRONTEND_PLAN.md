# AI Job Finder: Frontend Plan

A mobile-first web app where each person signs in with a link sent by the Telegram bot (no passwords) and gets
three feeds (For You, Internships, 2027 Batch) plus a tracker for every job they have applied to and its stage.
It reads the same `private/jobs.db` the daily run already writes, so the pipeline does not change.

Status: plan only, nothing built yet. First user: a 2027 graduate who needs internships and 2027-batch openings.

## What already exists

- `matches.decision` (none / applied / skipped). The Telegram buttons and the website will update the same field.
- `digest.py` already finds internships and 2027-batch jobs for `india_internships.csv` and `graduates_2027.csv`.
  These become two of the feeds.
- `jobs.lifecycle` (open / possibly_closed / closed) lets the site warn "this job may have closed".

## 1. Tech stack

| Part | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | Same language as the rest of the project. It imports `store.py` and `models.py` directly. |
| Frontend | Jinja templates + HTMX + Tailwind | No separate JS app to build or deploy. Buttons update in place without page reloads, and it's quick on phones. |
| Mobile | PWA (installable) | Can be added to the home screen and opens like an app. |
| Login | Magic link through Telegram | Type your name, the bot sends a one-time link, and you're in. Uses the existing `telegram_chat_id`. |
| Hosting (v1) | This Mac + Cloudflare Tunnel or Tailscale Funnel | Free and needs no data copying. The Mac has to be on for the 6:45 run anyway. |
| Hosting (later) | A small VPS, or syncing to Turso/Supabase | Only needed if "Mac must be awake" becomes a problem. |

Alternative: FastAPI as a JSON API with React + Vite + shadcn/ui. The screens stay the same. For a tool used by
2–5 people, HTMX is about half the work.

## 2. Screens

### A. Today (home page)

```
┌──────────────────────────────────┐
│ Hi there 👋   12 new since Tue    │
│ This week: 6 applied · goal 10   │
│ ▓▓▓▓▓▓░░░░                        │
├──────────────────────────────────┤
│ [For You] [Internships] [2027]   │
├──────────────────────────────────┤
│ ● STRONG  SDE Intern – Summer 27 │
│ Atlassian · Bengaluru · Hybrid   │
│ ₹80k/mo · posted 2d ago          │
│ ✓ Java, DSA   ✗ Kotlin           │
│ "Asks for 2027 batch, your stack"│
│ [Apply ↗] [✓ Applied] [☆] [✕]    │
├──────────────────────────────────┤
│ ⚠ You applied to Nasdaq 5d ago   │
│ ● GOOD  Campus Program 2027 ...  │
└──────────────────────────────────┘
```

- **For You**: AI-scored matches with the fit badge, the reason, and matched and missing skills.
- **Internships** and **2027 Batch**: the rule-based lists that already exist, filtered to the person's cities.
- Each card has four buttons: Apply (opens the link), Applied, Save, Skip.
- Jobs already applied to or skipped are hidden. A "show all" toggle brings them back.

### B. Job detail

- Full description, AI reason, matched and missing skills, red flags.
- Eligibility facts: batch years (from `rules.py`), experience range, stipend or salary, work mode.
- A "may be closed" banner when lifecycle is `possibly_closed` or `closed`.
- "Also at this company": other open roles there, plus ones already applied to.
- A notes box, e.g. "used referral from a friend".

### C. My Applications (the tracker)

```
 Saved(4) │ Applied(18) │ Test/OA(3) │ Interview(1) │ Offer │ Rejected(5)
 ─────────┼─────────────┼────────────┼──────────────┼───────┼────────────
 [card]   │ [card]      │ [card]     │ [card]       │       │ [card]
          │ Nasdaq 5d   │ OA due Fri │ R2 on 3 Nov  │       │
```

- A board view on desktop and a list grouped by stage on phones.
- Each application stores: stage, applied date, how they applied (portal, referral, campus), notes, next step and
  its date.
- A timeline shows each stage change with its date.
- Applications with no reply after 21 days are marked "Ghosted?".
- Reminders go out through the Telegram bot, e.g. "OA for Atlassian is due tomorrow".

### D. Search all jobs

- Searches every stored job, replacing the CSV.
- Filters: internship or full-time, batch year, city, work mode, posted within N days, company, stipend range,
  open only.

### E. My Profile

- Edit target roles, skills, cities, work modes, and the "hide jobs with these words" lists.
- Paste or upload a resume as text.
- Profiles move from YAML into the database (or the site writes the YAML) so each person can edit their own.

### F. Stats

- Applications per week, the funnel from applied to OA to interview to offer, and response rate.
- Which sources gave jobs that got replies. This shows which collectors are worth the effort.

### G. Admin (owner only)

- People list, an "add person" form, and whether each person's daily list was sent.
- Source health from `source_runs`: failing or suspicious sources and how long each scan took.

## 3. Features that matter most for a 2027 student

1. **Duplicate-application warning**: "You applied to 2 roles at Nasdaq already."
2. **Batch-year badge** on every card ("2027 ✓", "2025/26 only ✗") to avoid applying to jobs they aren't eligible for.
3. **Closing-soon / may-be-closed nudges** for saved jobs not applied to yet.
4. **Weekly goal and streak**, such as 10 applications a week.
5. **"Not relevant" feedback with a reason** ("needs 2 yrs", "not software"), reviewed to improve rules and prompts.
6. **Telegram and web stay in sync**: tapping ✅ in Telegram shows as Applied on the site, and the other way round.

Later: extracting application deadlines, a prep-links box per company, a referral contacts list.

## 4. Data changes (small, additive)

```
applications        one row per (profile, job): stage, applied_at, method, notes,
                    next_step, next_step_at        (stage "applied" also sets matches.decision)
application_events  history: application_id, from_stage, to_stage, at
saved               or a "saved" value in matches.decision
login_tokens        one-time magic-link tokens with an expiry
sessions            signed cookie, so this may need no table at all
feedback            profile_id, job_key, reason
profiles            move the YAML fields here (phase 3)
```

Internship and 2027 jobs that the AI never scored for a person have no row in `matches`, so `applications` keys on
`job_key + profile_id`, not `match_id`.

## 5. Build phases

| Phase | What ships | Effort |
|---|---|---|
| 1. MVP | Telegram magic-link login, Today with 3 feeds, job detail, Applied/Save/Skip synced with Telegram, simple Applied list, running on Cloudflare/Tailscale | ~1 week |
| 2. Tracker | Stages, notes, timeline, OA/interview reminders on Telegram, duplicate-company warning, Search all jobs | ~1 week |
| 3. Self-serve | Profile editor (profiles in DB), stats page, weekly goal, admin and source health | ~1 week |
| 4. Polish | PWA install, dark mode, "not relevant" feedback loop, optional move off this Mac | as needed |

## 6. Privacy rules from day 1

- Every query filters by the signed-in person's `profile_id`.
- No one sees another person's resume or tracker.
- `private/` stays git-ignored.
- The site runs only over HTTPS through the tunnel.
