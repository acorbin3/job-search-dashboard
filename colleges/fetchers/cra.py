"""CRA (Computing Research Association) Career Center: https://careercenter.cra.org

cra.org/ads/ redirects here. Plain GET `/jobs?keywords=<q>&page=<n>` returns
server-rendered cards (a[data-testid="jobs-list-item-link"] inside
div.card-body, with the employer and location in the two .small lines under
it). Sponsored "Spotlight" cards repeat on every page, so we dedupe by URL and
stop when a page adds nothing new. Detail pages carry a schema.org JobPosting
JSON-LD with description, datePosted, hiringOrganization and jobLocation.
Every posting here is in computing, so the queries are role words only.
"""
import html as _html
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean
from .madgex import NON_US

name = "cra"
needs_browser = False
BASE = "https://careercenter.cra.org"
QUERIES = ["lecturer", "instructor", "adjunct", "teaching professor", "visiting", "professor of practice",
           "online", "part-time"]
MAX_PAGES = 4
MAX_DETAILS = 150
DELAY = 1.0


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _cards(html):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[data-testid='jobs-list-item-link'][href]"):
        card = a.find_parent("div", class_="card-body") or a.parent
        smalls = [clean(x.get_text(" ", strip=True)) for x in card.select(".location .small")]
        yield {
            "title": clean(a.get_text(" ", strip=True)),
            "url": urljoin(BASE, a["href"]).split("?")[0],
            "institution": smalls[0] if smalls else "",
            "location": smalls[1] if len(smalls) > 1 else "",
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
        if isinstance(j, list):
            j = next((x for x in j if isinstance(x, dict) and x.get("@type") == "JobPosting"), None)
        if not isinstance(j, dict) or j.get("@type") != "JobPosting":
            continue
        org = j.get("hiringOrganization") or {}
        addr = ((j.get("jobLocation") or {}).get("address") or {}) if isinstance(j.get("jobLocation"), dict) else {}
        loc = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x)
        return (strip_html(j.get("description") or ""), (j.get("datePosted") or "")[:10],
                clean(_html.unescape(org.get("name", ""))) if isinstance(org, dict) else "", loc)
    body = soup.select_one(".job-description, #job-description") or soup.find("main")
    return (strip_html(str(body)) if body else ""), "", "", ""


def fetch(config):
    out, seen, hits = [], set(), []
    session = requests.Session()
    session.headers.update(HEADERS)
    for q in QUERIES:
        for page in range(1, MAX_PAGES + 1):
            try:
                r = session.get(f"{BASE}/jobs", params={"keywords": q, "page": page}, timeout=30)
            except Exception:
                break
            if r.status_code != 200:
                break
            new = 0
            for c in _cards(r.text):
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
                desc, posted, inst, loc = _detail(session, c["url"])
            except Exception:
                pass
            time.sleep(DELAY)
        cfg = dict(config)
        cfg["name"] = inst or c["institution"] or cfg.get("name", "")
        try:
            out.append(make_job(cfg, "college_cra", c["title"], c["url"], loc or c["location"], desc, posted))
        except Exception:
            continue
    return out
