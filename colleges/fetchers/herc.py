"""HERC (Higher Education Recruitment Consortium) aggregator: https://main.hercjobs.org

Plain requests get a "Please click to confirm you are not a robot" page. The
check is purely client-side: the page embeds `humanCheckToken = "..."`, the JS
waits >= 2 s (hcheckMinDwellMs) and sets cookie `hcheck=<token>` then reloads.
Replaying that (extract token, sleep 2.5 s, set cookie) unlocks the whole site
for the session, so no Playwright is needed. /jobs/rss/ is NOT a feed (it
redirects to the WordPress marketing site), so we scrape the search HTML.

Search: /jobs/?keywords=<q>&page=<n>; cards are div.bti-job-search-row with
.card-title a (title/link), .card-subtitle (institution), .card-text (location).
Detail pages embed a schema.org JobPosting JSON-LD block with description,
datePosted, hiringOrganization and jobLocation.
"""
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "herc"
needs_browser = False
BASE = "https://main.hercjobs.org"
QUERIES = [
    "adjunct computer science",
    "adjunct information technology",
    "adjunct cybersecurity",
    "instructor computer science",
    "lecturer computer science",
    "faculty cybersecurity",
]
MAX_PAGES = 3          # per query
MAX_DETAILS = 150      # bounds runtime (~1 req/s) when title patterns are broad
DETAIL_DELAY = 1.0     # seconds between detail fetches (~1 req/s)
_TOKEN_RE = re.compile(r'humanCheckToken\s*=\s*"([^"]+)"')


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _is_challenge(text):
    return "humanCheckToken" in text or "not a robot" in text.lower()


def _get(session, url, params=None, tries=2):
    """GET that transparently solves HERC's cookie challenge."""
    for _ in range(tries):
        r = session.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return None
        if not _is_challenge(r.text):
            return r
        m = _TOKEN_RE.search(r.text)
        if not m:
            return None
        time.sleep(2.5)  # server enforces the page's 2 s minimum dwell
        session.cookies.set("hcheck", m.group(1), domain="main.hercjobs.org", path="/")
    return None


def _cards(html):
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("div.bti-job-search-row"):
        a = row.select_one(".card-title a[href]")
        if not a:
            continue
        inst = row.select_one(".card-subtitle")
        loc = row.select_one(".card-text")
        yield {
            "title": clean(a.get_text(" ", strip=True)),
            "url": urljoin(BASE, a["href"]),
            "institution": clean(inst.get_text(" ", strip=True)) if inst else "",
            "location": clean(loc.get_text(" ", strip=True)) if loc else "",
        }


def _detail(session, url):
    """Return (description, posted, institution, location) from the JSON-LD."""
    r = _get(session, url)
    if r is None:
        return "", "", "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text())
        except Exception:
            continue
        if isinstance(j, list):
            j = next((x for x in j if isinstance(x, dict) and x.get("@type") == "JobPosting"), None)
        if not isinstance(j, dict) or j.get("@type") != "JobPosting":
            continue
        desc = strip_html(j.get("description") or "")
        resp = strip_html(j.get("responsibilities") or "")
        if resp and resp not in desc:
            desc = f"{desc}\n\n{resp}".strip()
        posted = (j.get("datePosted") or "")[:10]
        org = j.get("hiringOrganization") or {}
        inst = clean(org.get("name", "")) if isinstance(org, dict) else ""
        addr = ((j.get("jobLocation") or {}).get("address") or {}) if isinstance(j.get("jobLocation"), dict) else {}
        loc = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x)
        return desc, posted, inst, loc
    main = soup.select_one("#bti-search-detail") or soup.find("main") or soup.body
    return (strip_html(str(main)) if main else ""), "", "", ""


def fetch(config):
    out, seen = [], set()
    session = requests.Session()
    session.headers.update(HEADERS)
    hits = []
    for q in QUERIES:
        for page in range(1, MAX_PAGES + 1):
            try:
                r = _get(session, f"{BASE}/jobs/", params={"keywords": q, "page": page})
            except Exception:
                r = None
            if r is None:
                break
            new = 0
            for c in _cards(r.text):
                if c["url"] in seen:
                    continue
                seen.add(c["url"])
                new += 1
                if title_ok(c["title"]):
                    hits.append(c)
            if new == 0:
                break
            time.sleep(DETAIL_DELAY)
    for i, c in enumerate(hits):
        desc, posted, inst, loc = "", "", "", ""
        if i < MAX_DETAILS:
            try:
                desc, posted, inst, loc = _detail(session, c["url"])
            except Exception:
                pass
            time.sleep(DETAIL_DELAY)
        inst = inst or c["institution"]
        loc = loc or c["location"]
        if loc and inst and loc.lower() == inst.lower():
            loc = ""  # some cards repeat the employer name in the location slot
        try:
            job = make_job(config, "college_herc", c["title"], c["url"], loc, desc, posted)
        except Exception:
            continue
        job.company = inst or job.company
        out.append(job)
    return out
