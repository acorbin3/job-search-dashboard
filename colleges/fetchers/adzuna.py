"""Adzuna US job search (aggregator; plain requests work, no API key needed).

  GET https://www.adzuna.com/search?q=<q>&p=N
cards: article[data-aid] with h2 a[href=/details/<id>], .ui-company,
.ui-location, .ui-salary and a .max-snippet-height snippet. 10 per page.
Detail pages (/details/<id>) embed a JobPosting JSON-LD (description,
datePosted, hiringOrganization, jobLocation).
"""
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "adzuna"
needs_browser = False
BASE = "https://www.adzuna.com"
QUERIES = ["adjunct computer science", "adjunct information technology", "adjunct cybersecurity",
           "instructor computer science", "lecturer computer science", "adjunct data science",
           "adjunct programming"]
MAX_PAGES = 10
MAX_DETAILS = 150
DELAY = 1.0


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _cards(html):
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("article[data-aid]"):
        a = card.select_one("h2 a[href*='/details/']")
        if not a:
            continue
        c, l, s = card.select_one(".ui-company"), card.select_one(".ui-location"), card.select_one(".max-snippet-height")
        yield {"title": clean(a.get_text(" ", strip=True)), "url": a["href"].split("?")[0],
               "company": clean(c.get_text(" ", strip=True)) if c else "",
               "loc": clean(l.get_text(" ", strip=True)) if l else "",
               "snippet": clean(s.get_text(" ", strip=True)) if s else ""}


def _detail(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return "", "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text())
        except Exception:
            continue
        nodes = j if isinstance(j, list) else [j]
        for n in nodes:
            if isinstance(n, dict) and n.get("@type") == "JobPosting":
                org = n.get("hiringOrganization") or {}
                return (strip_html(n.get("description") or ""), (n.get("datePosted") or "")[:10],
                        clean(org.get("name", "")) if isinstance(org, dict) else "")
    body = soup.select_one(".adp-body")
    return (strip_html(str(body)) if body else ""), "", ""


def _title_case(s):
    return " ".join(w.capitalize() if w.isupper() and len(w) > 3 else w for w in s.split())


def fetch(config):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        seen, hits = set(), []
        for q in QUERIES:
            for page in range(1, MAX_PAGES + 1):
                try:
                    r = session.get(f"{BASE}/search", params={"q": q, "p": page}, timeout=30)
                except Exception:
                    break
                if r.status_code != 200:
                    break
                cards = list(_cards(r.text))
                new = 0
                for c in cards:
                    if c["url"] in seen:
                        continue
                    seen.add(c["url"])
                    new += 1
                    if title_ok(c["title"]):
                        hits.append(c)
                time.sleep(DELAY)
                if not cards or new == 0 or not re.search(rf"[?&]p={page + 1}\b", r.text):
                    break
        out = []
        for i, c in enumerate(hits):
            desc, posted, org = "", "", ""
            if i < MAX_DETAILS:
                try:
                    desc, posted, org = _detail(session, c["url"])
                except Exception:
                    pass
                time.sleep(DELAY)
            company = org or _title_case(c["company"]) or config.get("name", "")
            cfg = dict(config, name=company)
            try:
                out.append(make_job(cfg, "college_adzuna", c["title"], c["url"], _title_case(c["loc"]),
                                    desc or c["snippet"], posted))
            except Exception:
                continue
        return out
    except Exception:
        return []
