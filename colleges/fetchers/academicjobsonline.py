"""AcademicJobsOnline aggregator: https://academicjobsonline.org/ajo/jobs

The HTML listing obfuscates its filter links (every anchor is /ajo/lkrdt#N,
rewritten by JS from a cdnURLs table) and the RSS feed is a gzip'd RDF blob,
but the site also publishes its full position list as JSON for reuse:

    GET /ajo/public_job_boards?limit=100&page=N
    (optional: search=<kw>, date_from=, date_to=, sort_by=, xml=1, csv=1)

Each record carries univ, unit_name, name (title), location/city/state/
country, type, subject, open_date_raw, deadline_raw, url and the full HTML
description + qualifications, so no per-job detail request is needed.
robots.txt asks for Crawl-delay: 5; we make ~10 paged requests per run and
sleep between them.
"""
import time

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "academicjobsonline"
needs_browser = False
FEED = "/ajo/public_job_boards"
PAGE_SIZE = 100
MAX_PAGES = 30
DELAY = 1.2          # seconds between requests


def detect(url, html=""):
    return None


def _date(s):
    s = (s or "").strip()
    return "" if not s or s.startswith("0000") else s[:10]


def _location(it):
    loc = clean(it.get("location") or "")
    if not loc:
        loc = ", ".join(p for p in (clean(it.get("city")), clean(it.get("state"))) if p)
    country = clean(it.get("country") or "")
    if country and country.upper() not in ("US", "USA"):
        loc = f"{loc}, {country}" if loc else country
    return loc


def _description(it):
    parts = []
    head = [("Position type", it.get("type")), ("Subject", it.get("subject")),
            ("Department", it.get("unit_name")), ("Deadline", _date(it.get("deadline_raw"))),
            ("Salary", it.get("salary"))]
    for k, v in head:
        if clean(v):
            parts.append(f"{k}: {clean(v)}")
    for key in ("description", "qualifications", "instructions"):
        txt = strip_html(it.get(key) or "")
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)


def fetch(config):
    base = (config.get("url") or "https://academicjobsonline.org").rstrip("/")
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        try:
            r = requests.get(f"{base}{FEED}", params={"limit": PAGE_SIZE, "page": page},
                             headers=HEADERS, timeout=30)
            if r.status_code != 200:
                break
            data = r.json()
        except Exception:
            break
        results = data.get("results") or []
        for it in results:
            title = clean(it.get("name") or "")
            url = (it.get("url") or "").strip()
            if not url and it.get("id"):
                url = f"{base}/ajo/jobs/{it['id']}"
            if not url or url in seen or not title_ok(title):
                continue
            seen.add(url)
            job = make_job(config, "college_academicjobsonline", title, url,
                           _location(it), _description(it), _date(it.get("open_date_raw")),
                           clean(it.get("unit_name") or ""))
            inst = clean(it.get("univ") or "")
            if inst:
                job.company = inst
            out.append(job)
        try:
            total = int(data.get("total_count") or 0)
        except (TypeError, ValueError):
            total = 0
        if len(results) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
            break
        time.sleep(DELAY)
    return out
