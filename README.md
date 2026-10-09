# Job dashboard

A local web app that scrapes the public job-board APIs of ~12,000 companies
(Greenhouse, Lever, Ashby, Workday, SmartRecruiters, and 20 other ATS hosts),
filters the postings down to the roles you actually want, scores each one
0–100 against **your resume**, and tracks what you've applied to.

Everything runs on your machine. No accounts, no SaaS, one SQLite file.

## Quick start

```bash
git clone https://github.com/acorbin3/job-search-dashboard.git
cd job-search-dashboard
pip install -r requirements.txt     # flask, requests, beautifulsoup4 (+ optional pypdf, anthropic)
python3 server.py                   # http://127.0.0.1:5050/
```

The first visit lands on the setup page:

1. **Upload your resume** (PDF, DOCX, TXT or TEX). It is turned into a *fit
   profile* — a table of title levels, skill keywords, and company sectors —
   that drives the Fit column. The file never leaves your machine unless you
   choose the Claude method (below).
2. **Pick a starter tab** — Senior Software Engineer, Staff Engineer,
   Engineering Manager, Director of Engineering, Product Manager, Project /
   Program Manager, ML Engineer, Data Engineer — or write your own title
   rules. A tab is a saved search: regexes a job title must match (level +
   domain) and must not match (exclude).
3. Open the tab and hit **↻ Refresh data**. The first full scan takes 10–20
   minutes; it caches a raw superset so later rule changes re-derive in
   seconds (**⟳ Re-parse**).

## How fit scoring works

Each job gets points for how close its title's seniority is to yours, for
every keyword hit in each of your skill domains (capped per domain), for the
company's sector, and for being remote or near your home location. The table
is plain JSON you can read and edit on the setup page (`/tabs#resume`):

```json
{
  "label": "My resume (2026)",
  "level":   [["\\bsenior\\b", 25], ["\\bstaff\\b", 18]],        // [title regex, points]; first match wins
  "domains": [["\\bfhir\\b|\\bhl7\\b|\\behr\\b", 4, 20]],        // [regex, points per hit, cap]
  "sector":  {"healthtech": 8, "data-infra": 4},
  "remote_bonus": 8,
  "location_re": "austin|texas|\\btx\\b",
  "location_bonus": 8
}
```

Two ways to build it from a resume:

- **Keyword heuristic (default, local).** Matches the resume against a fixed
  taxonomy of skill groups, infers your level from the titles near the top,
  and reads the home city off the header. Good enough to start; edit the JSON
  to taste.
- **Claude.** `pip install anthropic`, set `ANTHROPIC_API_KEY` (or run
  `ant auth login`), restart the server, and pick *Claude* as the method. The
  resume text is sent to the Anthropic API and the returned table goes
  through the same validation. Override the model with `RESUME_FIT_MODEL`.

You can keep several fit profiles (one per resume variant) and pin a tab to
a specific one; everything else uses the default.

## What else is in here

| Page | What it does |
|---|---|
| `/dashboard/<tab>` | The results table: filter by remote, stage, sector, pay, fit; star / ignore / mark applied; Glassdoor ratings fill in the background |
| `/tabs` | Create and edit tabs; upload resumes and manage fit profiles |
| `/applied` | Applications per day, pipeline stage, interviews |

Companion scripts in `tools/`: `add_workday.py` (add a Workday tenant),
`li_session_from_chrome.py` and `gd_session_from_chrome.py` (lift your browser
session for the LinkedIn crawler and Glassdoor ratings). `linkedin_scrape.py`
and `glassdoor.py` are the crawlers themselves.

## Files you own vs. files in the repo

Written by the UI and gitignored: `profiles.json` (your tabs),
`fit_profiles.json` (your scoring tables), `resumes/` (uploads), `state.db`
(applications, triage, caches), `.latest_*.json` / `.raw_scan.json` (scan
results), `profile.json` (autofill identity — copy from `profile.example.json`).

In the repo: `data/companies.json` (the company list), `company_meta.py`
(stage/sector per company), `data/profile_templates.json` (the starter tabs),
and the code. See `CLAUDE.md` for the architecture.

## Tests

```bash
python3 -m pytest
```
