"""YourMembership (YM Careers / Community Brands) boards behind Cloudflare:
ACM Career & Job Center (jobs.acm.org), IEEE Job Site (jobs.ieee.org) and
EDUCAUSE Career Center (jobs.educause.edu). Config: {"url": "<base>"}.

These are NOT Madgex boards (no /jobsrss/). Plain requests get a Cloudflare
"Just a moment" 403, but one headless-Chromium load of <base>/jobs/ solves
the challenge; replaying that context's cookies with the same User-Agent lets
plain requests fetch `/jobs/?keywords=<q>&page=<n>` (25 per page, pinned
sponsored cards repeat on every page) and the detail pages. Cards are
div.job-details: .job-title a (/job/<slug>/<id>/), .job-company-row,
.job-location, .job-posted-date (relative). Detail pages embed a JSON-LD
@graph holding a JobPosting (description, datePosted, hiringOrganization,
jobLocation); fallback is .job-description-parent-container. Should the cookie
replay fail, pages are rendered in the browser instead (slower, no details).
"""
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean
from .madgex import NON_US

name = "ymcareers"
needs_browser = True
QUERIES = ["adjunct computer science", "adjunct information technology", "adjunct cybersecurity",
           "instructor computer science", "lecturer computer science", "faculty cybersecurity",
           "online faculty computer science", "adjunct data science", "part-time faculty computer"]
MAX_PAGES = 2
MAX_DETAILS = 150
DELAY = 1.0
UA = HEADERS["User-Agent"].replace("124.0.0.0", "130.0.0.0")
JOB_RE = re.compile(r"/job/[^/]+/\d+/?$")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _challenge(text):
    return "Just a moment" in text[:6000] or "cf-browser-verification" in text[:6000]


class _Client:
    """requests with Cloudflare cookies seeded by Playwright; browser fallback."""

    def __init__(self, base):
        self.base = base
        self.session = requests.Session()
        self.session.headers.update({**HEADERS, "User-Agent": UA})
        self.pw = self.browser = self.page = None
        self.use_browser = False
        try:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
            ctx = self.browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900})
            self.page = ctx.new_page()
            self.page.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|css)(\?|$)", re.I), lambda r: r.abort())
            self.page.goto(f"{base}/jobs/", wait_until="domcontentloaded", timeout=30000)
            for _ in range(10):
                if not _challenge(self.page.content()):
                    break
                self.page.wait_for_timeout(2000)
            for c in ctx.cookies():
                self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        except Exception:
            self.use_browser = False

    def get(self, url, params=None):
        if not self.use_browser:
            try:
                r = self.session.get(url, params=params, timeout=30)
                if r.status_code == 200 and not _challenge(r.text):
                    return r.text
            except Exception:
                pass
            self.use_browser = True        # cookies rejected: render everything from now on
        if self.page is None:
            return ""
        try:
            full = url if not params else requests.Request("GET", url, params=params).prepare().url
            self.page.goto(full, wait_until="domcontentloaded", timeout=30000)
            for _ in range(10):
                if not _challenge(self.page.content()):
                    break
                self.page.wait_for_timeout(2000)
            return self.page.content()
        except Exception:
            return ""

    def close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass


def _cards(html, base):
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("div.job-details"):
        a = card.select_one(".job-title a[href]")
        if not a or not JOB_RE.search(a["href"]):
            continue
        loc_el = card.select_one(".job-location")
        loc = ""
        if loc_el:
            for junk in loc_el.select("input, .workplace-location"):
                junk.decompose()
            loc = clean(loc_el.get_text(" ", strip=True))
        inst = card.select_one(".job-company-row")
        yield {
            "title": clean(a.get_text(" ", strip=True)),
            "url": urljoin(base, a["href"]),
            "institution": clean(inst.get_text(" ", strip=True)) if inst else "",
            "location": re.sub(r",\s*United States$", "", loc),
        }


def _detail(client, url):
    html = client.get(url)
    if not html:
        return "", "", "", ""
    soup = BeautifulSoup(html, "html.parser")
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text(), strict=False)
        except Exception:
            continue
        nodes = j.get("@graph", [j]) if isinstance(j, dict) else (j if isinstance(j, list) else [])
        jp = next((x for x in nodes if isinstance(x, dict) and x.get("@type") == "JobPosting"), None)
        if not jp:
            continue
        org = jp.get("hiringOrganization") or {}
        addr = ((jp.get("jobLocation") or {}).get("address") or {}) if isinstance(jp.get("jobLocation"), dict) else {}
        loc = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x)
        return (strip_html(jp.get("description") or ""), (jp.get("datePosted") or "")[:10],
                clean(org.get("name", "")) if isinstance(org, dict) else "", loc)
    body = soup.select_one(".job-description-parent-container, #main-job-details")
    return (strip_html(str(body)) if body else ""), "", "", ""


def fetch(config):
    base = (config.get("url") or "").rstrip("/")
    if not base:
        return []
    out, seen, hits = [], set(), []
    client = None
    try:
        client = _Client(base)
        for q in QUERIES:
            for page in range(1, MAX_PAGES + 1):
                html = client.get(f"{base}/jobs/", params={"keywords": q, "page": page})
                if not html:
                    break
                new = 0
                for c in _cards(html, base):
                    if c["url"] in seen:
                        continue
                    seen.add(c["url"])
                    new += 1
                    if NON_US.search(c["location"]):
                        continue
                    if title_ok(c["title"]):
                        hits.append(c)
                time.sleep(DELAY)
                if new == 0:
                    break
        for i, c in enumerate(hits):
            desc, posted, inst, loc = "", "", "", ""
            if i < MAX_DETAILS:
                try:
                    desc, posted, inst, loc = _detail(client, c["url"])
                except Exception:
                    pass
                time.sleep(DELAY)
            cfg = dict(config)
            cfg["name"] = inst or c["institution"] or cfg.get("name", "")
            try:
                out.append(make_job(cfg, "college_ymcareers", c["title"], c["url"], loc or c["location"], desc, posted))
            except Exception:
                continue
    except Exception:
        pass
    finally:
        if client:
            client.close()
    return out
