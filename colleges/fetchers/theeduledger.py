"""TheEduLedger jobs (formerly Diverse: Issues In Higher Education / DiverseJobs):
https://jobs.theeduledger.com  (diversejobs.net and jobs.diverseeducation.com
now redirect here; it is a Recruitology/Jobcase board, not Madgex).

`/search?keyword=<q>&page=<n>` is a shell whose results are pulled by an XHR
guarded with an Altcha proof-of-work header, so listing pages are rendered in
headless Chromium (wait for div.job-item). Each item has h3.job-title a
(title + /company/<org>/job/<slug>/?sid=), a.company-name and
span.item-location[data-location]. Detail pages are plain HTML with a
JobPosting JSON-LD (description, datePosted, hiringOrganization), fetched with
requests.
"""
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "theeduledger"
needs_browser = True
BASE = "https://jobs.theeduledger.com"
QUERIES = ["adjunct computer science", "adjunct information technology", "adjunct cybersecurity",
           "adjunct software", "adjunct data science", "instructor computer science",
           "lecturer computer science", "faculty cybersecurity", "online faculty computer science"]
MAX_PAGES = 2
MAX_DETAILS = 150
DELAY = 1.0
UA = HEADERS["User-Agent"].replace("124.0.0.0", "130.0.0.0")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _items(html):
    soup = BeautifulSoup(html, "html.parser")
    for it in soup.select("div.job-item"):
        a = it.select_one("h3.job-title a[href]")
        if not a:
            continue
        inst = it.select_one("a.company-name")
        loc = it.select_one(".item-location")
        yield {
            "title": clean(a.get_text(" ", strip=True)),
            "url": a["href"].split("?")[0],
            "institution": clean(inst.get_text(" ", strip=True)) if inst else "",
            "location": clean(loc.get("data-location") or loc.get_text(" ", strip=True)) if loc else "",
        }


def _detail(session, url):
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return "", "", "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text(), strict=False)
        except Exception:
            continue
        if not isinstance(j, dict) or j.get("@type") != "JobPosting":
            continue
        org = j.get("hiringOrganization") or {}
        addr = ((j.get("jobLocation") or {}).get("address") or {}) if isinstance(j.get("jobLocation"), dict) else {}
        loc = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x)
        return (strip_html(j.get("description") or ""), (j.get("datePosted") or "")[:10],
                clean(org.get("name", "")) if isinstance(org, dict) else "", loc)
    body = soup.select_one(".job-description, #job-description") or soup.find("main")
    return (strip_html(str(body)) if body else ""), "", "", ""


def fetch(config):
    out, seen, hits = [], set(), []
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            page.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4)(\?|$)", re.I), lambda r: r.abort())
            for q in QUERIES:
                for n in range(1, MAX_PAGES + 1):
                    try:
                        page.goto(f"{BASE}/search?keyword={requests.utils.quote(q)}&page={n}",
                                  wait_until="domcontentloaded", timeout=30000)
                        try:
                            page.wait_for_selector("div.job-item", timeout=20000)
                        except Exception:
                            break
                        html = page.content()
                    except Exception:
                        break
                    new = 0
                    for it in _items(html):
                        if it["url"] in seen:
                            continue
                        seen.add(it["url"])
                        new += 1
                        if title_ok(it["title"]):
                            hits.append(it)
                    time.sleep(DELAY)
                    if new == 0:
                        break
            browser.close()
    except Exception:
        pass
    session = requests.Session()
    session.headers.update(HEADERS)
    for i, it in enumerate(hits):
        desc, posted, inst, loc = "", "", "", ""
        if i < MAX_DETAILS:
            try:
                desc, posted, inst, loc = _detail(session, it["url"])
            except Exception:
                pass
            time.sleep(DELAY)
        cfg = dict(config)
        cfg["name"] = inst or it["institution"] or cfg.get("name", "")
        try:
            out.append(make_job(cfg, "college_theeduledger", it["title"], it["url"], loc or it["location"], desc, posted))
        except Exception:
            continue
    return out
