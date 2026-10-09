# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A local Flask app that scrapes public job-board APIs for ~12,000 companies,
filters postings by configurable title rules ("tabs"), scores each one 0–100
against an uploaded resume ("fit profiles"), and tracks applications. Single
user, single machine, SQLite state. See `README.md` for the user-facing tour.

## Layout

```
server.py            Flask app: pages + JSON API; spawns scrape.py / linkedin_scrape.py
scrape.py            ATS fetchers (25 hosts), title filters, superset scan, fit scoring
fit_profiles.py      resume scoring tables: fit_profiles.json <-> compiled form
resume_fit.py        resume text extraction + heuristic / Claude derivation of a fit profile
glassdoor.py         background Glassdoor rating fetcher (curl + Chrome cookie jar)
linkedin_scrape.py   LinkedIn search crawler (needs a browser session)
applied_match.py     "already applied?" matcher for LinkedIn rows
applied_dedupe.py    repost de-duplication for the applied tracker
company_meta.py      slug -> (funding stage, sector) table; feeds dashboard filters
data/                companies.json (the company list), profile_templates.json
                     (starter tabs), profile.example.json (autofill identity template)
templates/ static/   Jinja pages + one stylesheet
tools/               add_workday.py, li_session_from_chrome.py, gd_session_from_chrome.py
extensions/          Chrome extensions: linkedin (seen/dismissed tracking), field-capture
tests/               pytest (python3 -m pytest)
```

Per-user files are written next to the code and gitignored: `profiles.json`
(tabs), `fit_profiles.json` (scoring tables), `resumes/`, `profile.json`,
`state.db`, `.latest_<tab>.json`, `.raw_scan.json`. Never add personal data,
tab keys, or a home town to code; it belongs in those files.

## Commands

```bash
python3 server.py                     # http://127.0.0.1:5050/
./server.sh start|stop|restart|logs   # same, backgrounded
python3 scrape.py --all               # one superset scan -> every tab's .latest_<tab>.json
python3 scrape.py --from-raw          # re-derive all tabs from the cache, no network
python3 scrape.py --profile <tab> --json out.json
python3 resume_fit.py resume.pdf --no-claude   # preview a derived fit profile
python3 tools/add_workday.py <slug> <tenant> <wd> <site>
python3 -m pytest
```

Deps: `requirements.txt` (flask, requests, beautifulsoup4; optional pypdf and
anthropic). No linter or build step.

## Architecture

**Pipeline.** `data/companies.json` (ATS name -> list of slugs; Workday
entries are `{slug, tenant, wd, site}` dicts; keys starting with `_` are
comments) -> `scrape.py` fetchers yield `Job` dataclasses -> title filters ->
`score_job()` (fit + healthcare signal) -> `.latest_<tab>.json` -> the
dashboard reads it via `GET /api/jobs/<tab>`. All table filtering/sorting is
client-side JS in `templates/dashboard.html`.

**Superset mode (`--all`)** is the primary refresh path: `set_superset_globals()`
unions every tab's level/domain patterns, one `scan()` collects a raw superset
(cached to `.raw_scan.json`, deduped by URL), then `apply_profile()` derives
each tab locally. `--from-raw` re-derives without network; the dashboard's
⟳ Re-parse button does this. Per-tab filters live in `apply_profile()`; the
in-fetcher filter chain only matters for single-tab runs.

**Tabs (role profiles)** live in `profiles.json`, managed from `/tabs`. There
are no built-in tabs: `scrape.load_profiles()` and `server.load_profiles()`
return `{}` when the file is missing, and `/` redirects to `/tabs?welcome=1`
until one exists. Starter tabs come from `data/profile_templates.json` via
`POST /api/profiles/from_template`. Each tab: `level` / `domain` / `exclude`
regex lists (matched against the title) plus optional flags —
`min_ai_hits`, `require_etl_signal`, `require_healthtech_sector`,
`health_adjacent_ok`, `require_remote_or_local` + `local_re` (blank = the fit
profile's home location), `hidden`, `fit` (pin a fit profile), `source`
(`ats` | `linkedin`), `remote_default`, `accenture_keyword`. The legacy
`require_remote_or_tampa` key is migrated on load. The active tab is applied
by mutating module globals in `scrape.main()`; nothing should hardcode a tab key.

**Fit profiles.** `fit_profiles.py` loads `fit_profiles.json` (`_default` names
the table used when no `--fit` is given) plus a bland built-in `generic`.
JSON shape: `level: [[regex, pts]]` (first match wins, title only),
`domains: [[regex, per_hit, cap]]` (title + description), `sector: {name:
pts}`, `remote_bonus`, `location_re`, `location_bonus`; `compile_spec()`
produces what `scrape.compute_fit()` consumes. In the server always go through
`fit_profiles.load()` / `labels()` / `default_key()` — `scrape.FIT_PROFILES`
is a snapshot at import. Fit is recomputed on `--from-raw`, so switching
profiles is a no-network re-derive. `apply_profile()` re-scores a tab in place
when it pins a `fit`.

**Resume upload.** `resume_fit.extract_text()` (pdf via pypdf or the
`pdftotext` CLI, docx via zipfile, tex stripped, txt/md) ->
`derive_heuristic()` (taxonomy `GROUPS`, title ladders `LADDERS`; only terms
the resume mentions enter a domain regex; level inferred from titles near the
top with adjacent matches merged into one title) or `derive_with_claude()`
(anthropic SDK, JSON-schema output, same regex builder) when
`claude_available()`. Routes: `GET /api/fit_profiles`,
`POST /api/fit_profiles/derive` (multipart; returns spec + summary, stores the
upload under `resumes/`), `POST /api/fit_profiles/<key>` (`{spec,
make_default, rescore}`), `DELETE`, `POST /api/fit_profiles/default`. UI:
the Resume & fit panel on `/tabs`. The dashboard header shows which profile
scored the tab.

**Data conventions.** Pay parsing (`PAY_RANGE_RE` + `extract_pay`) picks the
`$X - $Y` range with the highest max, skipping hourly/equity; Ashby's
structured `compensation` wins when present. `classify_remote()` checks
workplace type, then location string, then description. `company_meta.py`
returns `("", "")` for unknown slugs — when adding a company to
`data/companies.json`, add a META entry too or the stage/sector filters
miss it. US-HQ companies only; ATS slugs are often impostors, so verify a
board's identity before adding one.

**Workday.** `fetch_workday` walks whole boards (cap `WORKDAY_MAX_POSTINGS`);
its searchText is fuzzy-OR and cannot target roles. The browser network tab
is the source of truth for `tenant`/`wd`/`site`; `tools/add_workday.py`
verifies the endpoint returns jobs before writing.

**Glassdoor column.** One background worker + queue shared by every tab,
persisted in `state.db` (`glassdoor`, `glassdoor_queue`). Fetching goes
through `curl --http1.1` with cookies lifted from Chrome by
`tools/gd_session_from_chrome.py`; python `requests` gets 403'd by the bot
wall, so don't "simplify" it. A 403 means the cookie jar went stale: load a
Glassdoor page in Chrome, re-run the tool. Throughput tops out ~1.5
companies/min regardless of pacing.

**Sources.** A tab sets `ats` / `linkedin` booleans (legacy files use
`source: "linkedin"` for LinkedIn-only; `scrape.profile_sources()` normalizes).
The ATS scan writes `.latest_<key>.json`; the LinkedIn crawl
(`linkedin_scrape.py --out`) writes `.latest_<key>.linkedin.json` for a mixed
tab and `GET /api/jobs/<key>` merges them by URL. ↻ Refresh on a mixed tab
starts the ATS scan and then `POST /api/linkedin/crawl {use_tab_defaults}`,
which crawls the tab's `linkedin_keywords`. The 🔗 LinkedIn panel is the
manual/tuning path. LinkedIn tabs honor `require_desc` / `exclude_desc`
description rules. The signed-in (voyager)
endpoint is needed for boolean queries; `tools/li_session_from_chrome.py`
extracts the session.
