#!/usr/bin/env python3
"""
LinkedIn job crawler.

LinkedIn's own search UI has become unreliable (dedupes aggressively, caps the
visible result set, and reshuffles between page loads). This walks the public
*guest* job-search endpoint instead, which needs no cookie and pages 10 results
at a time up to start=990 (~1000 per query — a hard server-side ceiling, so
broad queries must be split into narrower ones to get full coverage).

Two endpoints, both unauthenticated:
  search  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
  detail  https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>

The search response is a bare <li> list of job cards; the detail response is a
fragment with the full description and the "Seniority level / Employment type /
Job function / Industries" criteria block.

Filters that DO pass through to the guest endpoint: keywords, geoId, location,
f_TPR (date posted), f_WT (workplace type), f_E (experience level), f_JT (job
type), f_C (company ids).  Filters that are silently IGNORED: f_SAL (salary) —
verified by diffing result ids with and without it.  Salary is therefore
applied locally with --min-pay, parsed out of the description by the same
extract_pay() the ATS scraper uses.

Output is a list of scrape.Job records written to .latest_<profile>.json, so
the existing Flask dashboard renders LinkedIn hits with the same Fit column,
filters, and apply-tracking as ATS hits.

Usage
-----
    # Paste a LinkedIn search URL straight from the browser (repeatable)
    python3 linkedin_scrape.py --url "https://www.linkedin.com/jobs/search-results/?keywords=..."

    # Or build the query by hand
    python3 linkedin_scrape.py --keywords "Senior Engineering Manager" \
        --keywords "Director of Engineering" --remote --posted-days 7

    # Title-filter through an existing dashboard profile, require $225k+
    python3 linkedin_scrape.py --url "..." --title-profile em --min-pay 225000

Detail pages are cached in state.db (linkedin_jobs), so re-runs only fetch
postings that are new since last time.
"""

import argparse
import html
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import applied_dedupe
import scrape
from company_meta import get_meta

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "state.db"

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"

# The guest endpoints 999/429 a scanner-looking UA, so present as a browser.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PAGE_SIZE = 10        # fixed by the endpoint
MAX_START = 990       # start=1000 returns HTTP 400

GEO_US = "103644278"  # United States

# f_WT (workplace type) and f_E (experience level) codes, for --remote/--level.
WT_ONSITE, WT_REMOTE, WT_HYBRID = "1", "2", "3"
EXPERIENCE_LEVELS = {
    "internship": "1", "entry": "2", "associate": "3",
    "mid-senior": "4", "director": "5", "executive": "6",
}

# Search params worth carrying over from a pasted browser URL. f_SAL is
# deliberately absent — the guest endpoint ignores it (see module docstring).
PASSTHROUGH_PARAMS = {
    "keywords", "location", "geoId", "f_TPR", "f_WT", "f_E", "f_JT", "f_C",
    "f_I", "f_F", "sortBy", "distance",
}


# ---------------------------------------------------------------------------
# HTTP with backoff. LinkedIn answers overload with 429 or its custom 999.
# ---------------------------------------------------------------------------

_throttle = threading.Lock()
_last_request = [0.0]

# Global inter-request gap, shared by every worker. Adaptive: a 429/999 widens
# it and it decays back toward the floor over a run of clean responses, so a
# long crawl self-tunes to whatever LinkedIn is tolerating right now rather
# than hammering at a fixed rate until it gets banned.
MIN_INTERVAL = 1.43       # floor (~0.7 req/s), overridden by --rate
_interval = [MIN_INTERVAL]
MAX_INTERVAL = 30.0
BACKOFF_GROWTH = 2.5      # multiply the gap on each throttle
RECOVERY_DECAY = 0.97     # shrink it 3% per clean response
_clean_streak = [0]
_throttle_events = [0]


def set_rate(per_second):
    """Set the request-rate floor from --rate."""
    global MIN_INTERVAL
    MIN_INTERVAL = 1.0 / per_second if per_second > 0 else 0.8
    _interval[0] = MIN_INTERVAL


def _pace():
    """Serialize request starts so N workers don't burst together."""
    with _throttle:
        gap = time.time() - _last_request[0]
        wait = _interval[0] - gap
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


def _note_throttled():
    """Widen the global gap after a 429/999."""
    with _throttle:
        _throttle_events[0] += 1
        _clean_streak[0] = 0
        old = _interval[0]
        _interval[0] = min(_interval[0] * BACKOFF_GROWTH, MAX_INTERVAL)
        if _interval[0] > old:
            print(f"    ~ throttled; slowing {old:.1f}s -> {_interval[0]:.1f}s "
                  f"between requests", file=sys.stderr, flush=True)


def _note_ok():
    """Decay the gap back toward the floor after sustained clean responses."""
    with _throttle:
        _clean_streak[0] += 1
        if _clean_streak[0] >= 10 and _interval[0] > MIN_INTERVAL:
            _interval[0] = max(_interval[0] * RECOVERY_DECAY, MIN_INTERVAL)


def get(url, params=None, tries=5, timeout=25):
    """GET with adaptive pacing + exponential backoff. Text, or None if dead."""
    delay = 5.0
    for attempt in range(tries):
        _pace()
        try:
            r = requests.get(url, params=params, headers=HEADERS,
                             timeout=timeout)
        except requests.RequestException as e:
            if attempt == tries - 1:
                print(f"    ! network error: {e}", file=sys.stderr)
                return None
            time.sleep(delay + random.uniform(0, 1))
            delay *= 2
            continue
        if r.status_code == 200:
            _note_ok()
            return r.text
        if r.status_code in (400, 404):
            return None            # past the end of the result set / gone
        if r.status_code in (429, 999, 403) or r.status_code >= 500:
            _note_throttled()
            if attempt == tries - 1:
                print(f"    ! giving up on HTTP {r.status_code}", file=sys.stderr)
                return None
            # Honor Retry-After when LinkedIn sends one, else exponential.
            retry_after = r.headers.get("Retry-After")
            pause = delay
            if retry_after and retry_after.isdigit():
                pause = max(delay, min(int(retry_after), 120))
            time.sleep(pause + random.uniform(0, 2))
            delay *= 2
            continue
        return None
    return None


def throttle_report():
    return (f"{_throttle_events[0]} throttle event(s); "
            f"final gap {_interval[0]:.2f}s between requests")


# ---------------------------------------------------------------------------
# Progress. The log tail alone leaves long silences (a query pages up to 1000
# results, and detail batches only print every 25), so the crawler also keeps a
# small machine-readable state file the server serves to the dashboard's
# progress bar. Best-effort: a failed write never interrupts a crawl.
# ---------------------------------------------------------------------------

# One file per tab, so a manager / SWE / adjunct crawl each report their own
# progress instead of overwriting a single shared file. main() repoints this
# once --profile is resolved; the bare name is the legacy default.
PROGRESS_PATH = ROOT / ".linkedin_crawl_progress.json"


def progress_path_for(profile):
    return ROOT / f".linkedin_crawl_progress_{profile}.json"

# Search is the cheap half of a typical run; details dominate wall clock.
SEARCH_WEIGHT = 0.35

_progress = {
    "phase": "starting", "label": "", "queries_done": 0, "queries_total": 0,
    "cards": 0, "details_done": 0, "details_total": 0, "published": 0,
    "pct": 0, "eta_sec": None, "elapsed_sec": 0,
    # One entry per keyword, updated in place. A 20-keyword run rewrote the
    # single `label` line constantly (keyword, then page, then next keyword),
    # which read as flicker; a stable list where only the active row moves is
    # legible at any keyword count. `label` stays for older dashboards.
    "queries": [],
}
_progress_lock = threading.Lock()
_progress_t0 = [time.time()]
_details_t0 = [None]
_progress_written = [0.0]


def _overall_pct(p):
    """Blend the two phases into one 0-100 bar."""
    qt, dt = p["queries_total"], p["details_total"]
    search = (p["queries_done"] / qt) if qt else 0.0
    if p["phase"] in ("starting", "search"):
        return int(100 * SEARCH_WEIGHT * search)
    if p["phase"] == "filter":
        return int(100 * SEARCH_WEIGHT)
    if p["phase"] == "done":
        return 100
    detail = (p["details_done"] / dt) if dt else 1.0
    return int(100 * (SEARCH_WEIGHT + (1 - SEARCH_WEIGHT) * detail))


def init_queries(labels):
    """Seed the per-keyword rows, all pending."""
    with _progress_lock:
        _progress["queries"] = [
            {"label": l, "state": "pending", "cards": 0, "page": 0}
            for l in labels
        ]


def query_state(i, **fields):
    """Update one keyword's row in place (no-op if the index is unknown)."""
    with _progress_lock:
        rows = _progress["queries"]
        if 0 <= i < len(rows):
            rows[i].update(fields)


def progress(force=False, **fields):
    """Merge fields into the progress file (rate-limited unless forced)."""
    with _progress_lock:
        _progress.update(fields)
        if _progress["phase"] == "details" and _details_t0[0] is None:
            _details_t0[0] = time.time()
        now = time.time()
        _progress["elapsed_sec"] = int(now - _progress_t0[0])
        _progress["pct"] = _overall_pct(_progress)
        _progress["throttles"] = _throttle_events[0]
        _progress["interval"] = round(_interval[0], 2)
        done, total = _progress["details_done"], _progress["details_total"]
        if _progress["phase"] == "details" and done and done < total:
            per = (now - _details_t0[0]) / done
            _progress["eta_sec"] = int(per * (total - done))
        elif _progress["phase"] == "done":
            _progress["eta_sec"] = 0
        if not force and now - _progress_written[0] < 0.4:
            return
        _progress_written[0] = now
        # Copy the rows too: json.dumps runs outside the lock, and a shallow
        # copy would let a worker mutate a row mid-serialize.
        snapshot = dict(_progress, updated=datetime.now(timezone.utc)
                        .isoformat(timespec="seconds"),
                        queries=[dict(r) for r in _progress["queries"]])
    try:
        tmp = PROGRESS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot))
        tmp.replace(PROGRESS_PATH)
    except OSError:
        pass                       # progress is cosmetic; never fail a crawl


# ---------------------------------------------------------------------------
# Parsing. Regex rather than a DOM parser: `requests` is this repo's only dep,
# and the guest markup is a stable server-rendered template (unlike the logged-
# in SPA, whose class names are hashed and rotate).
# ---------------------------------------------------------------------------

CARD_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
CARD_SPLIT_RE = re.compile(r"<li>", re.I)

TITLE_RE = re.compile(
    r'class="base-search-card__title"[^>]*>\s*(.*?)\s*</h3>', re.S)
COMPANY_RE = re.compile(
    r'class="base-search-card__subtitle".*?<a[^>]*>\s*(.*?)\s*</a>', re.S)
COMPANY_URL_RE = re.compile(
    r'class="base-search-card__subtitle".*?<a[^>]*href="([^"]+)"', re.S)
LOCATION_RE = re.compile(
    r'class="job-search-card__location"[^>]*>\s*(.*?)\s*</span>', re.S)
LISTDATE_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
LINK_RE = re.compile(r'class="base-card__full-link"[^>]*href="([^"]+)"')

CRITERIA_RE = re.compile(
    r'job-criteria__subheader[^>]*>\s*(.*?)\s*</h3>.*?'
    r'job-criteria__text[^"]*"[^>]*>\s*(.*?)\s*</span>', re.S)
DESC_RE = re.compile(
    r'show-more-less-html__markup(.*?)(?:</div>\s*</section>|</div>\s*<section)',
    re.S)
APPLICANTS_RE = re.compile(r'num-applicants__caption[^>]*>\s*([^<]+)')


def _text(s):
    """Unescape + collapse whitespace on an HTML fragment's inner text."""
    return scrape.strip_html(s) if s else ""


def parse_cards(page_html):
    """Pull (id, title, company, location, url, posted_at) from a result page."""
    out = []
    for chunk in CARD_SPLIT_RE.split(page_html):
        m = CARD_RE.search(chunk)
        if not m:
            continue
        job_id = m.group(1)
        link = LINK_RE.search(chunk)
        # Strip the tracking query string — it changes every request and would
        # otherwise defeat url-keyed dedupe against the state table.
        url = html.unescape(link.group(1)).split("?")[0] if link else \
            f"https://www.linkedin.com/jobs/view/{job_id}"
        company_url = COMPANY_URL_RE.search(chunk)
        listdate = LISTDATE_RE.search(chunk)
        out.append({
            "id": job_id,
            "title": _text(TITLE_RE.search(chunk).group(1))
                     if TITLE_RE.search(chunk) else "",
            "company": _text(COMPANY_RE.search(chunk).group(1))
                       if COMPANY_RE.search(chunk) else "",
            "company_url": (html.unescape(company_url.group(1)).split("?")[0]
                            if company_url else ""),
            "location": _text(LOCATION_RE.search(chunk).group(1))
                        if LOCATION_RE.search(chunk) else "",
            "url": url,
            "posted_at": listdate.group(1) if listdate else "",
        })
    return out


def parse_detail(page_html):
    """Pull description text + criteria block out of a job detail fragment."""
    desc_m = DESC_RE.search(page_html)
    description = _text(desc_m.group(1)) if desc_m else ""
    criteria = {}
    for key, val in CRITERIA_RE.findall(page_html):
        criteria[_text(key)] = _text(val)
    applicants = APPLICANTS_RE.search(page_html)
    return {
        "description": description,
        "criteria": criteria,
        "applicants": _text(applicants.group(1)) if applicants else "",
    }


# ---------------------------------------------------------------------------
# Voyager (signed-in) search.
#
# The guest endpoint is purely semantic: it ignores quoting, OR/NOT and even
# sortBy, so a boolean query there scores no better than the bare words and
# results always come back in relevance order. Measured on one keyword:
# quoting reshuffled the set but left the count and the off-target hits alone,
# a NOT clause *added* noise, and sortBy=DD returned byte-identical ids. The
# signed-in endpoint honors all three, pages 25 at a time instead of 10, and
# reports a real total.
#
# It needs the browser session cookies, so it is opt-in (--voyager) and falls
# back to the guest path when no session is configured or the cookies expire.
# ---------------------------------------------------------------------------

VOYAGER_URL = "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards"
VOYAGER_DECORATION = ("com.linkedin.voyager.dash.deco.jobs.search."
                      "JobSearchCardsCollection-220")
VOYAGER_PAGE_SIZE = 25          # fixed by the endpoint's count=
SESSION_PATH = Path.home() / ".config" / "healthtech-jobs" / "linkedin_session.json"


class SessionExpired(Exception):
    """The configured li_at/JSESSIONID no longer authenticates."""


# Set once a run has given up on voyager, so the remaining queries go straight
# to the guest path instead of re-failing an auth handshake per query.
_voyager_off = [False]


def load_session(path=SESSION_PATH):
    """Browser cookies for the voyager endpoint, or None if not configured.

    Reads li_at + JSESSIONID from $LINKEDIN_LI_AT/$LINKEDIN_JSESSIONID, else
    from linkedin_session.json. JSESSIONID doubles as the CSRF token: the
    header value must match the cookie or every call comes back 403.
    """
    li_at = os.environ.get("LINKEDIN_LI_AT", "")
    jsess = os.environ.get("LINKEDIN_JSESSIONID", "")
    if not (li_at and jsess):
        try:
            blob = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            blob = {}
        li_at = li_at or blob.get("li_at", "")
        jsess = jsess or blob.get("JSESSIONID") or blob.get("jsessionid") or ""
    jsess = str(jsess).strip('"')
    if not (li_at and jsess):
        return None
    # The cookie carries the quotes; the header must not.
    return {"cookies": {"li_at": li_at, "JSESSIONID": f'"{jsess}"'},
            "csrf": jsess}


def _rison(value):
    """Percent-encode one value inside voyager's query= mini-syntax.

    ( ) , and : are structural there, so they have to be escaped even though
    they are legal URL characters. Getting this wrong does not error — a NOT
    clause with bare parens silently matches nothing at all.
    """
    return urllib.parse.quote(str(value), safe="")


def voyager_query_string(params):
    """Render guest-style params as the voyager query= argument."""
    filters = []
    for key, name in (("sortBy", "sortBy"), ("f_TPR", "timePostedRange"),
                      ("f_WT", "workplaceType"), ("f_E", "experience"),
                      ("f_JT", "jobType"), ("f_C", "company")):
        if params.get(key):
            # Commas already separate multi-select values; they are structural
            # inside List(...) so they stay unescaped.
            filters.append(f"{name}:List({params[key]})")
    geo = params.get("geoId") or GEO_US
    bits = ["origin:JOB_SEARCH_PAGE_OTHER_ENTRY",
            f"keywords:{_rison(params.get('keywords', ''))}",
            f"locationUnion:(geoUrn:{_rison(f'urn:li:fsd_geo:{geo}')})"]
    if filters:
        bits.append("selectedFilters:(" + ",".join(filters) + ")")
    bits.append("spellCorrectionEnabled:true")
    return "(" + ",".join(bits) + ")"


def voyager_get(url, session, tries=4, timeout=25):
    """GET returning parsed JSON. Raises SessionExpired on a dead cookie."""
    headers = {**HEADERS, "csrf-token": session["csrf"],
               "accept": "application/json",
               "x-restli-protocol-version": "2.0.0"}
    delay = 5.0
    for attempt in range(tries):
        _pace()
        try:
            r = requests.get(url, headers=headers, cookies=session["cookies"],
                             timeout=timeout)
        except requests.RequestException as e:
            if attempt == tries - 1:
                print(f"    ! network error: {e}", file=sys.stderr)
                return None
            time.sleep(delay + random.uniform(0, 1))
            delay *= 2
            continue
        if r.status_code == 200:
            _note_ok()
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code in (401, 403):
            raise SessionExpired(f"HTTP {r.status_code}")
        if r.status_code in (400, 404):
            return None            # past the end of the result set
        if r.status_code in (429, 999) or r.status_code >= 500:
            _note_throttled()
            if attempt == tries - 1:
                print(f"    ! giving up on HTTP {r.status_code}",
                      file=sys.stderr)
                return None
            retry_after = r.headers.get("Retry-After")
            pause = delay
            if retry_after and retry_after.isdigit():
                pause = max(delay, min(int(retry_after), 120))
            time.sleep(pause + random.uniform(0, 2))
            delay *= 2
            continue
        return None
    return None


def parse_voyager_cards(payload):
    """Map a voyager page onto the same dicts parse_cards() returns."""
    out = []
    for el in payload.get("elements") or []:
        card = (el.get("jobCardUnion") or {}).get("jobPostingCard") or {}
        posting = card.get("jobPosting") or {}
        urn = posting.get("entityUrn") or card.get("jobPostingUrn") or ""
        job_id = urn.rsplit(":", 1)[-1]
        if not job_id:
            continue
        posted = ""
        for item in card.get("footerItems") or []:
            if item.get("type") == "LISTED_DATE" and item.get("timeAt"):
                posted = datetime.fromtimestamp(
                    item["timeAt"] / 1000, timezone.utc).strftime("%Y-%m-%d")
                break
        out.append({
            "id": job_id,
            "title": posting.get("title") or card.get("jobPostingTitle") or "",
            "company": (card.get("primaryDescription") or {}).get("text", ""),
            # Not in the card payload; the detail fetch fills it in later.
            "company_url": "",
            "location": (card.get("secondaryDescription") or {}).get("text", ""),
            "url": f"https://www.linkedin.com/jobs/view/{job_id}",
            "posted_at": posted,
        })
    return out


def crawl_query_voyager(params, max_results, seen_ids, session, on_page=None):
    """crawl_query()'s signed-in twin. Same contract, 25-per-page."""
    found = []
    empty_pages = 0
    ceiling = min(max_results, MAX_START + VOYAGER_PAGE_SIZE)
    for start in range(0, ceiling, VOYAGER_PAGE_SIZE):
        if on_page:
            on_page(len(found), start)
        payload = voyager_get(
            f"{VOYAGER_URL}?decorationId={VOYAGER_DECORATION}"
            f"&count={VOYAGER_PAGE_SIZE}&q=jobSearch"
            f"&query={voyager_query_string(params)}&start={start}", session)
        if payload is None:
            break
        cards = parse_voyager_cards(payload)
        if not cards:
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue
        empty_pages = 0
        fresh = [c for c in cards if c["id"] not in seen_ids]
        for c in fresh:
            seen_ids.add(c["id"])
        found.extend(fresh)
        if not fresh:
            break
        # Unlike the guest endpoint, this one reports a real total, so a run
        # can stop on the last page instead of probing for two empty ones.
        total = (payload.get("paging") or {}).get("total")
        if total is not None and start + VOYAGER_PAGE_SIZE >= total:
            break
    return found


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

def parse_search_url(url):
    """Turn a pasted linkedin.com/jobs/search URL into guest-API params."""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    params = {}
    for key, vals in qs.items():
        if key in PASSTHROUGH_PARAMS and vals and vals[0]:
            params[key] = vals[0]
    if "f_SAL" in qs:
        print("  note: f_SAL (salary) is ignored by the guest endpoint — "
              "use --min-pay to filter locally instead.", file=sys.stderr)
    if not params.get("keywords"):
        print(f"  warning: no keywords in {url[:80]}", file=sys.stderr)
    params.setdefault("geoId", GEO_US)
    return params


SEARCHES_FILE = ROOT / "linkedin_searches.json"


def load_searches(path=SEARCHES_FILE):
    """Named searches from linkedin_searches.json ('_' keys are comments)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items()
            if not k.startswith("_") and isinstance(v, dict)}


def apply_search(args, name, searches):
    """Fill argparse defaults from a named search, without clobbering flags.

    Anything the caller passed explicitly stays put; the search only supplies
    what was left at its default. Keywords are additive so
    `--search adjunct --keywords "..."` widens rather than replaces.
    """
    cfg = searches.get(name)
    if cfg is None:
        raise SystemExit(f"unknown --search {name!r}; "
                         f"known: {', '.join(sorted(searches)) or '(none)'}")
    # `include` borrows another search's queries. A narrow tab is usually the
    # broad tab's crawl seen through a stricter filter, and re-listing a dozen
    # boolean queries just to change the filter invites them to drift apart.
    # One level only — an included search's own `include` is ignored.
    borrowed = []
    for other in cfg.get("include") or []:
        if other not in searches:
            raise SystemExit(f"--search {name!r} includes unknown search {other!r}")
        borrowed += list(searches[other].get("keywords") or [])
    args.keywords = (args.keywords or []) + borrowed + list(cfg.get("keywords") or [])
    for key in ("profile", "title_profile", "posted_days", "min_pay", "seed_from"):
        if key in cfg and not getattr(args, key, None):
            setattr(args, key, cfg[key])
    if cfg.get("remote"):
        args.remote = True
    return cfg


def build_queries(args):
    """Every query to crawl, as a list of guest-API param dicts."""
    queries = [parse_search_url(u) for u in (args.url or [])]
    for kw in (args.keywords or []):
        queries.append({"keywords": kw, "geoId": args.geo})
    if not queries:
        raise SystemExit("nothing to crawl: pass --url and/or --keywords")

    for q in queries:
        # CLI flags override whatever came in on a pasted URL.
        if args.remote:
            q["f_WT"] = WT_REMOTE
        if args.posted_days:
            q["f_TPR"] = f"r{args.posted_days * 86400}"
        if args.level:
            q["f_E"] = ",".join(EXPERIENCE_LEVELS[l] for l in args.level)
        q.setdefault("geoId", args.geo)
    return queries


def query_label(q):
    bits = [q.get("keywords", "(no keywords)")]
    if q.get("f_WT") == WT_REMOTE:
        bits.append("remote")
    if q.get("f_TPR"):
        bits.append(q["f_TPR"])
    return " | ".join(bits)


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def crawl_query(params, max_results, seen_ids, on_page=None, session=None):
    """Page one query to exhaustion. Returns new card dicts (not yet detailed).

    on_page(found_so_far, start) fires after each page so a long-running query
    shows movement instead of going quiet for minutes.

    With a session, pages the signed-in endpoint (which honors boolean
    keywords and sortBy); without one, or once the cookies stop
    authenticating, falls back to the guest endpoint.
    """
    if session and not _voyager_off[0]:
        try:
            return crawl_query_voyager(params, max_results, seen_ids,
                                       session, on_page)
        except SessionExpired as e:
            _voyager_off[0] = True
            print(f"    ! voyager rejected the session ({e}) — re-copy li_at "
                  f"and JSESSIONID into {SESSION_PATH}. Falling back to the "
                  f"guest endpoint, which ignores boolean keywords and "
                  f"sortBy.", file=sys.stderr)
    found = []
    empty_pages = 0
    for start in range(0, min(max_results, MAX_START + PAGE_SIZE), PAGE_SIZE):
        if on_page:
            on_page(len(found), start)
        page = get(SEARCH_URL, params={**params, "start": start})
        if page is None:
            break
        cards = parse_cards(page)
        if not cards:
            # One blank page is normal mid-run; two in a row means the end.
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue
        empty_pages = 0
        fresh = [c for c in cards if c["id"] not in seen_ids]
        for c in fresh:
            seen_ids.add(c["id"])
        found.extend(fresh)
        # A full page of already-seen ids means the result set has started
        # repeating (LinkedIn recycles once it runs out of distinct hits).
        if not fresh:
            break
    return found


def fetch_details(cards, workers, on_batch=None, batch_size=25):
    """Fetch detail pages concurrently, mutating each card in place.

    Calls on_batch() every batch_size completions so the caller can publish
    partial results — a long crawl fills the dashboard as it goes instead of
    showing nothing until the last fetch lands.
    """
    done = [0]
    lock = threading.Lock()

    def one(card):
        page = get(DETAIL_URL.format(card["id"]))
        card.update(parse_detail(page) if page else
                    {"description": "", "criteria": {}, "applicants": ""})
        with lock:
            done[0] += 1
            finished = done[0]
        progress(details_done=finished, details_total=len(cards))
        if finished % batch_size == 0 or finished == len(cards):
            print(f"    details {finished}/{len(cards)}", flush=True)
            if on_batch:
                # Serialized: the writer touches every card, and a partial
                # write racing another would corrupt the published file.
                with lock:
                    on_batch(finished, len(cards))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, cards))
    return cards


# ---------------------------------------------------------------------------
# Detail cache (state.db). Descriptions are the expensive part of a run and
# never change, so cache them and only fetch ids we've never detailed.
# ---------------------------------------------------------------------------

def init_cache(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_jobs (
            linkedin_id  TEXT PRIMARY KEY,
            url          TEXT,
            company      TEXT,
            title        TEXT,
            location     TEXT,
            posted_at    TEXT,
            description  TEXT,
            criteria     TEXT,
            applicants   TEXT,
            first_seen_at TEXT,
            last_seen_at  TEXT
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(linkedin_jobs)")}
    if "company_url" not in cols:
        conn.execute("ALTER TABLE linkedin_jobs ADD COLUMN company_url TEXT")
    # Which tab(s) each job was crawled for. linkedin_jobs is one shared
    # description cache across every tab (re-crawling a job elsewhere should
    # not refetch it), but the merge-with-earlier-crawls step has to stay
    # inside one tab — otherwise every LinkedIn tab publishes every job ever
    # crawled that happens to pass its title filter, and the healthcare tab
    # fills up with the engineering tab's results.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_job_tabs (
            linkedin_id TEXT,
            profile     TEXT,
            seen_at     TEXT,
            PRIMARY KEY (linkedin_id, profile)
        )
    """)
    conn.commit()


def load_all_cards(conn, max_age_days, profile):
    """Everything crawled *for this tab*, so a re-run adds instead of replacing.

    Without this, crawling a different keyword set wipes the previous results
    off the dashboard — the rows still exist in state.db but stop being
    published. Rows not re-seen within max_age_days age out.

    Scoped to `profile`: the cache is shared across tabs, but carrying another
    tab's history into this one makes the tabs converge on the same list.
    """
    cutoff = ""
    if max_age_days:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT j.linkedin_id, j.url, j.company, j.title, j.location, "
        "       j.posted_at, j.description, j.criteria, j.applicants, "
        "       j.company_url "
        "FROM linkedin_jobs j "
        "JOIN linkedin_job_tabs t ON t.linkedin_id = j.linkedin_id "
        "WHERE t.profile = ? AND j.last_seen_at >= ?", (profile, cutoff))
    return [{"id": r[0], "url": r[1], "company": r[2] or "", "title": r[3] or "",
             "location": r[4] or "", "posted_at": r[5] or "",
             "description": r[6] or "", "criteria": json.loads(r[7] or "{}"),
             "applicants": r[8] or "", "company_url": r[9] or ""}
            for r in rows]


def load_cached(conn, ids):
    """{id: card-ish dict} for ids we've already fetched details for."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 400):     # stay under SQLite's var limit
        chunk = ids[i:i + 400]
        q = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT linkedin_id, description, criteria, applicants "
            f"FROM linkedin_jobs WHERE linkedin_id IN ({q}) "
            f"AND description != ''", chunk
        ):
            out[row[0]] = {
                "description": row[1],
                "criteria": json.loads(row[2] or "{}"),
                "applicants": row[3] or "",
            }
    return out


def save_cache(conn, cards, profile):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO linkedin_jobs
            (linkedin_id, url, company, title, location, posted_at,
             description, criteria, applicants, company_url,
             first_seen_at, last_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(linkedin_id) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            location     = excluded.location,
            description  = CASE WHEN excluded.description != ''
                                THEN excluded.description ELSE description END,
            criteria     = CASE WHEN excluded.description != ''
                                THEN excluded.criteria ELSE criteria END
    """, [(c["id"], c["url"], c["company"], c["title"], c["location"],
           c.get("posted_at", ""), c.get("description", ""),
           json.dumps(c.get("criteria", {})), c.get("applicants", ""),
           c.get("company_url", ""), now, now) for c in cards])
    # Claim these jobs for this tab, so the next run's merge picks them up.
    conn.executemany(
        "INSERT INTO linkedin_job_tabs (linkedin_id, profile, seen_at) "
        "VALUES (?,?,?) ON CONFLICT(linkedin_id, profile) "
        "DO UPDATE SET seen_at = excluded.seen_at",
        [(c["id"], profile, now) for c in cards])
    conn.commit()


def dismissed_ids(conn):
    """LinkedIn ids the browser extension recorded as dismissed."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT linkedin_id FROM linkedin_seen WHERE action = 'dismissed'")}
    except sqlite3.OperationalError:
        return set()          # extension never ran; table not created yet


# ---------------------------------------------------------------------------
# Enrichment — reuse the ATS scraper's parsers so LinkedIn rows are scored and
# shaped identically to greenhouse/lever/etc. rows.
# ---------------------------------------------------------------------------

SLUG_RE = re.compile(r"[^a-z0-9]+")
MORE_LOCS_RE = re.compile(r"\s*\(\+\d+ more\)\s*$")


def company_slug(name, company_url=""):
    """Best-effort slug for a META lookup: prefer LinkedIn's own /company/ slug."""
    if company_url:
        m = re.search(r"/company/([^/?]+)", company_url)
        if m:
            return m.group(1).lower()
    return SLUG_RE.sub("", (name or "").lower())


def to_job(card):
    """Card + detail -> scrape.Job, with pay/remote/AI/fit derived as usual."""
    desc = card.get("description", "")
    slug = company_slug(card["company"], card.get("company_url", ""))
    stage, sector = get_meta(slug)
    if not stage and not sector:
        # LinkedIn slugs often carry a suffix ("acme-health-inc"); retry on the
        # bare alphanumeric form of the display name.
        stage, sector = get_meta(SLUG_RE.sub("", (card["company"] or "").lower()))

    pay_min, pay_max, pay_text = scrape.extract_pay(desc)
    ai_hits, ai_snippet = scrape.find_ai_hits(desc)
    job = scrape.Job(
        company=card["company"] or slug,
        ats="linkedin",
        title=card["title"],
        location=card["location"],
        url=card["url"],
        ai_hits=ai_hits,
        ai_hit_count=len(ai_hits),
        snippet=ai_snippet or desc[:300],
        description=desc,
        posted_at=card.get("posted_at", ""),
        updated_at="",
        pay_min=pay_min,
        pay_max=pay_max,
        pay_text=pay_text,
        remote=scrape.classify_remote(card["location"], desc),
        stage=stage,
        sector=sector,
    )
    scrape.score_job(job)
    return job


def dedupe_reposts(cards):
    """Collapse the same role listed under several job ids.

    LinkedIn re-posts a role every few weeks with a fresh id, and a company
    fanning one opening out across metros yields a row per city. Key on
    (company, title), keep the first, and fold the other cities into its
    location so the extra metros aren't lost.

    Runs on search cards, before detail fetching — collapsing 111 duplicates
    here saves 111 description fetches rather than discarding them after.
    """
    best, locations = {}, {}
    for c in cards:
        # Normalized key, not the raw strings: a repost often comes back with
        # "Sr." for "Senior", a "(Remote)" suffix, or a comma where a dash was.
        key = applied_dedupe.repost_key(c["company"], c["title"])
        # Strip any suffix a previous pass added — publish() re-runs this on
        # every incremental flush, and appending twice would compound.
        loc = MORE_LOCS_RE.sub("", c["location"]).strip()
        c["location"] = loc
        locations.setdefault(key, []).append(loc)
        best.setdefault(key, c)
    for key, card in best.items():
        others = {l for l in locations[key] if l and l != card["location"]}
        if others:
            card["location"] = f"{card['location']} (+{len(others)} more)"
    return list(best.values())


def title_filter(profile_key):
    """Predicate applying a profiles.json entry's level/domain/exclude regexes."""
    cfg = scrape.PROFILES[profile_key]
    level, domain = cfg.get("level", []), cfg.get("domain", [])
    exclude = cfg.get("exclude", [])
    return lambda t: scrape.title_matches_for(t, level, domain, exclude)


def desc_rules(profile_key):
    """A profile's optional description-level rules, compiled.

    Some things a title can't tell you — whether an online teaching job is
    asynchronous or expects you on Zoom every Tuesday is stated in the body,
    if anywhere. `require_desc` keeps a role only if the description matches at
    least one pattern; `exclude_desc` drops it if any pattern matches.
    """
    cfg = scrape.PROFILES.get(profile_key) or {}
    return ([re.compile(p, re.I) for p in cfg.get("require_desc", [])],
            [re.compile(p, re.I) for p in cfg.get("exclude_desc", [])])


def apply_desc_rules(jobs, require, exclude):
    """Filter on description text, surfacing the phrases that matched.

    Matched phrases are prepended to `ai_hits` so the dashboard's Hits column
    shows *why* a role qualified — on a tab built around one description
    property, that evidence is the whole point of the column.

    Detail arrives progressively, so a job whose description hasn't been
    fetched yet simply fails `require_desc` and shows up on a later publish.
    """
    if not require and not exclude:
        return jobs
    kept = []
    for job in jobs:
        desc = job.description or ""
        if any(rx.search(desc) for rx in exclude):
            continue
        markers = []
        for rx in require:
            m = rx.search(desc)
            if m:
                markers.append(" ".join(m.group(0).split()).lower())
        if require and not markers:
            continue
        if markers:
            job.ai_hits = list(dict.fromkeys(markers + list(job.ai_hits)))
            job.ai_hit_count = len(job.ai_hits)
        kept.append(job)
    return kept


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Crawl LinkedIn's public job search into the dashboard.")
    ap.add_argument("--url", action="append",
                    help="paste a linkedin.com/jobs search URL (repeatable)")
    ap.add_argument("--keywords", action="append",
                    help="search phrase (repeatable)")
    ap.add_argument("--geo", default=GEO_US,
                    help=f"geoId (default {GEO_US} = United States)")
    ap.add_argument("--remote", action="store_true",
                    help="restrict to remote roles (f_WT=2)")
    ap.add_argument("--posted-days", type=int, default=0,
                    help="only postings from the last N days (f_TPR)")
    ap.add_argument("--level", action="append", choices=sorted(EXPERIENCE_LEVELS),
                    help="experience level filter (repeatable)")
    ap.add_argument("--title-profile", choices=sorted(scrape.PROFILES),
                    help="keep only titles matching this profiles.json entry")
    ap.add_argument("--voyager", action="store_true",
                    help="use the signed-in endpoint, which honors boolean "
                         "keywords (quotes/OR/NOT) and sortBy — the guest one "
                         f"ignores both. Needs li_at + JSESSIONID in "
                         f"{SESSION_PATH} or $LINKEDIN_LI_AT/"
                         f"$LINKEDIN_JSESSIONID; falls back to guest without "
                         f"them.")
    ap.add_argument("--min-pay", type=int, default=0,
                    help="drop rows whose parsed max pay is under this")
    ap.add_argument("--require-pay", action="store_true",
                    help="with --min-pay, also drop rows with no parsed pay")
    ap.add_argument("--min-fit", type=int, default=0,
                    help="drop rows scoring under this fit")
    ap.add_argument("--max-per-query", type=int, default=1000,
                    help=f"cap results per query (endpoint max {MAX_START + PAGE_SIZE})")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent detail fetches (default 4; raise carefully)")
    ap.add_argument("--rate", type=float, default=0.7,
                    help="target requests/sec floor (default 0.7 — measured as "
                         "roughly the point LinkedIn stops throttling). The pacer "
                         "backs off automatically on 429/999 and decays back "
                         "toward this once responses are clean.")
    ap.add_argument("--keep-reposts", action="store_true",
                    help="don't collapse duplicate (company, title, location)")
    ap.add_argument("--fresh", action="store_true",
                    help="publish ONLY this run's hits, replacing the tab "
                         "(default is to merge with everything crawled before)")
    ap.add_argument("--seed-from", metavar="PROFILE",
                    help="also consider roles already crawled for another tab, "
                         "filtered by this run's rules (no refetch)")
    ap.add_argument("--max-age-days", type=int, default=60,
                    help="drop previously-crawled roles not re-seen in N days "
                         "(default 60; 0 = keep forever)")
    ap.add_argument("--no-detail", action="store_true",
                    help="skip description fetches — titles only, much faster")
    ap.add_argument("--include-dismissed", action="store_true",
                    help="keep jobs dismissed via the browser extension")
    # Left unset so --search can supply it; resolved to "linkedin" below.
    ap.add_argument("--profile", default=None,
                    help="write .latest_<profile>.json (default: linkedin)")
    ap.add_argument("--search",
                    help="load keywords + defaults from a named entry in "
                         "linkedin_searches.json (e.g. eng_leadership, "
                         "sse_health, adjunct). Its boolean keywords need "
                         "--voyager to take effect.")
    ap.add_argument("--list-searches", action="store_true",
                    help="print the named searches and exit")
    ap.add_argument("--json", help="also write the raw job list here")
    ap.add_argument("--fit", default=scrape.DEFAULT_FIT,
                    choices=sorted(scrape.FIT_PROFILES),
                    help="resume fit profile used for scoring")
    args = ap.parse_args()

    searches = load_searches()
    if args.list_searches:
        for key, cfg in sorted(searches.items()):
            print(f"{key:16s} {cfg.get('label', '')}  "
                  f"-> tab {cfg.get('profile', 'linkedin')}, "
                  f"{len(cfg.get('keywords') or [])} quer"
                  f"{'y' if len(cfg.get('keywords') or []) == 1 else 'ies'}")
        return
    if args.search:
        cfg = apply_search(args, args.search, searches)
        print(f"Search: {args.search} ({cfg.get('label', '')})")
        if not args.voyager:
            print("  warning: this search uses boolean keywords, which the "
                  "guest endpoint ignores. Add --voyager.", file=sys.stderr)
    args.profile = args.profile or "linkedin"
    global PROGRESS_PATH
    PROGRESS_PATH = progress_path_for(args.profile)

    # The target tab may pin its own resume ("fit" in profiles.json); an
    # explicit --fit on the command line still wins.
    tab_fit = (scrape.PROFILES.get(args.profile) or {}).get("fit")
    if tab_fit in scrape.FIT_PROFILES and args.fit == scrape.DEFAULT_FIT:
        args.fit = tab_fit
    scrape.set_fit_profile(args.fit)
    set_rate(args.rate)
    queries = build_queries(args)

    conn = sqlite3.connect(DB_PATH)
    init_cache(conn)

    t0 = time.time()
    _progress_t0[0] = t0
    seen_ids, cards = set(), []
    session = None
    if args.voyager:
        session = load_session()
        if session:
            print("Search endpoint: voyager (signed in) — boolean keywords "
                  "and sortBy are honored.")
        else:
            print(f"--voyager: no session found (looked at {SESSION_PATH} and "
                  f"$LINKEDIN_LI_AT/$LINKEDIN_JSESSIONID). Using the guest "
                  f"endpoint, which ignores quotes/OR/NOT and sortBy.",
                  file=sys.stderr)
    print(f"Crawling {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}...")
    init_queries([query_label(q) for q in queries])
    progress(force=True, phase="search", queries_total=len(queries),
             queries_done=0, label=query_label(queries[0]) if queries else "")
    # Ceiling on how far a query can page — the denominator for its share of
    # the bar. Queries usually run dry early, which just makes the bar jump.
    pages_hint = min(args.max_per_query, MAX_START + PAGE_SIZE)
    for i, q in enumerate(queries):
        label = query_label(q)
        query_state(i, state="active")
        progress(force=True, phase="search", label=label, queries_done=i)

        def on_page(n, start, _i=i, _l=label):
            # seen_ids already absorbs each page's fresh cards as they land.
            # queries_done carries a fraction mid-query so a one-keyword run
            # still moves the bar instead of sitting at 0% for minutes.
            query_state(_i, page=start // PAGE_SIZE + 1, cards=n)
            progress(phase="search", label=_l, cards=len(seen_ids),
                     queries_done=_i + min(start / max(pages_hint, 1), 0.95))

        got = crawl_query(q, args.max_per_query, seen_ids, on_page=on_page,
                          session=session)
        cards.extend(got)
        print(f"  {label}: {len(got)} new ({len(seen_ids)} unique so far)")
        query_state(i, state="done", cards=len(got))
        progress(force=True, phase="search", label=label, queries_done=i + 1,
                 cards=len(seen_ids))

    if not cards:
        print("No cards returned — LinkedIn may be rate-limiting; retry later.")
        progress(force=True, phase="done", label="no cards returned")
        return

    if not args.include_dismissed:
        skip = dismissed_ids(conn)
        before = len(cards)
        cards = [c for c in cards if c["id"] not in skip]
        if before != len(cards):
            print(f"Skipped {before - len(cards)} previously dismissed.")

    # Cheap card-level filters run BEFORE detail fetching. Both only need the
    # title, and fetching a description just to discard the row is the single
    # most expensive mistake this crawler can make — on a 10-keyword run it
    # was ~70% of all fetches.
    progress(force=True, phase="filter", label="filtering cards",
             cards=len(cards))
    if not args.keep_reposts:
        before = len(cards)
        cards = dedupe_reposts(cards)
        if before != len(cards):
            print(f"Collapsed {before - len(cards)} duplicate re-posts "
                  f"(saving {before - len(cards)} fetches).")
    if args.title_profile:
        keep = title_filter(args.title_profile)
        before = len(cards)
        cards = [c for c in cards if keep(c["title"])]
        print(f"Title filter ({args.title_profile}): {len(cards)} kept "
              f"({before - len(cards)} fetches skipped)")
    if not cards:
        print("Nothing survived the title filter.")
        progress(force=True, phase="done", label="nothing survived filters")
        return

    out = ROOT / f".latest_{args.profile}.json"

    # Snapshot previously-crawled roles once, up front: publish() runs on
    # worker threads during the detail phase, and a SQLite connection can only
    # be used from the thread that created it.
    prior_cards = ([] if args.fresh
                   else load_all_cards(conn, args.max_age_days, args.profile))
    if prior_cards:
        print(f"Merging with {len(prior_cards)} roles from earlier crawls "
              f"(last {args.max_age_days} days)")

    # A narrow tab is usually a broad tab's crawl under a stricter filter, and
    # a brand-new tab starts blind to everything already fetched. Seeding
    # borrows the other tab's history — descriptions are already cached, so the
    # only cost is running this run's filters over them.
    if args.seed_from and not args.fresh:
        seeded = load_all_cards(conn, args.max_age_days, args.seed_from)
        if args.title_profile:
            keep_seed = title_filter(args.title_profile)
            seeded = [c for c in seeded if keep_seed(c["title"])]
        known = {c["id"] for c in prior_cards} | {c["id"] for c in cards}
        seeded = [c for c in seeded if c["id"] not in known]
        if seeded:
            print(f"Seeded {len(seeded)} roles already crawled for "
                  f"{args.seed_from} (no refetch)")
            prior_cards += seeded
            save_cache(conn, seeded, args.profile)   # they belong to this tab now

    def publish():
        """Score + filter whatever detail we have and write the output file.

        Publishes this run's cards UNION everything previously crawled, so
        changing keywords adds roles to the tab instead of replacing them.
        """
        by_id = {c["id"]: c for c in cards}
        # Cards from this run win — they carry the newest detail.
        for c in prior_cards:
            by_id.setdefault(c["id"], c)
        merged = list(by_id.values())
        # Re-apply the card-level filters across the union. The identical pass
        # before the fetch is purely a cost optimization; this one decides what
        # actually gets published, so roles carried over from earlier crawls
        # are held to the current filter and deduped against this run's hits.
        if not args.keep_reposts:
            merged = dedupe_reposts(merged)
        if args.title_profile:
            keep_title = title_filter(args.title_profile)
            merged = [c for c in merged if keep_title(c["title"])]
        jobs = [to_job(c) for c in merged]
        if args.title_profile:
            jobs = apply_desc_rules(jobs, *desc_rules(args.title_profile))
        if args.min_pay:
            jobs = [j for j in jobs
                    if j.pay_max >= args.min_pay
                    or (j.pay_max == 0 and not args.require_pay)]
        if args.min_fit:
            jobs = [j for j in jobs if j.fit >= args.min_fit]
        jobs.sort(key=lambda j: (-j.fit, -j.pay_max))
        payload = json.dumps([asdict(j) for j in jobs], indent=2)
        # Write-then-rename: the dashboard polls this file, and a half-written
        # one would parse as invalid JSON.
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(payload)
        tmp.replace(out)
        if args.json:
            Path(args.json).write_text(payload)
        return jobs

    if args.no_detail:
        for c in cards:
            c.update(description="", criteria={}, applicants="")
        jobs = publish()
    else:
        cached = load_cached(conn, [c["id"] for c in cards])
        need = [c for c in cards if c["id"] not in cached]
        for c in cards:
            if c["id"] in cached:
                c.update(cached[c["id"]])
            else:
                c.setdefault("description", "")
        print(f"Details: {len(cached)} cached, {len(need)} to fetch")
        progress(force=True, phase="details", details_done=0,
                 details_total=len(need), cards=len(cards),
                 label=f"{len(cached)} cached, {len(need)} to fetch")
        jobs = publish()      # publish titles + cached rows immediately
        progress(force=True, published=len(jobs))
        if need:
            print(f"    (dashboard is live now with {len(jobs)} rows; "
                  f"descriptions fill in as they arrive)")

            def flush(done, total):
                published = publish()
                progress(force=True, phase="details", details_done=done,
                         details_total=total, published=len(published))
                print(f"      -> published {len(published)} rows "
                      f"({done}/{total} detailed)", flush=True)

            fetch_details(need, args.workers, on_batch=flush)
            jobs = publish()
        save_cache(conn, cards, args.profile)

    if args.min_pay:
        print(f"Pay filter (>= ${args.min_pay:,}) applied")
    if args.min_fit:
        print(f"Fit filter (>= {args.min_fit}) applied")
    progress(force=True, phase="done", published=len(jobs),
             label=f"{len(jobs)} jobs published")
    print(f"\n{len(jobs)} jobs -> {out.name} in {time.time() - t0:.0f}s "
          f"({throttle_report()})")
    if jobs:
        print("\nTop 10 by fit:")
        for j in jobs[:10]:
            pay = f"${j.pay_max // 1000}k" if j.pay_max else "-"
            print(f"  {j.fit:3d}  {pay:>6}  {j.company[:24]:24}  {j.title[:52]}")


if __name__ == "__main__":
    main()
