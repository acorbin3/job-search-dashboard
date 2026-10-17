"""Talent.com job search (aggregator; plain requests work, no key needed).

  GET https://www.talent.com/jobs?k=<q>&l=United+States&p=N   (l must be set,
  otherwise the server geo-locates the client and searches one city)
cards: article[data-testid=job-card-unified] inside div[data-new-id] with
.JobCard_title__*, .JobCard_company__*, .JobCard_location__* and a link
/view?id=<id>. Detail pages embed a JobPosting JSON-LD (description,
datePosted, hiringOrganization). 20 cards per page.
"""
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "talent"
needs_browser = False
BASE = "https://www.talent.com"
QUERIES = ["adjunct computer science", "adjunct information technology", "adjunct cybersecurity",
           "instructor computer science", "lecturer computer science", "adjunct data science"]
MAX_PAGES = 10
MAX_DETAILS = 150
DELAY = 1.0


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _cards(html):
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("article[data-testid='job-card-unified']"):
        t = card.select_one("[class*=JobCard_title]")
        a = card.select_one("a[href*='/view?id=']") or card.find_parent("div", attrs={"data-new-id": True})
        if not t or a is None:
            continue
        href = a.get("href") if a.name == "a" else f"/view?id={a.get('data-new-id')}"
        c = card.select_one("[class*=JobCard_company]")
        l = card.select_one("[class*=JobCard_location]")
        yield {"title": clean(t.get_text(" ", strip=True)), "url": urljoin(BASE, href),
               "company": clean(c.get_text(" ", strip=True)) if c else "",
               "loc": clean(l.get_text(" ", strip=True)) if l else ""}


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
        nodes = j.get("@graph", [j]) if isinstance(j, dict) else j
        for n in nodes or []:
            if isinstance(n, dict) and n.get("@type") == "JobPosting":
                org = n.get("hiringOrganization") or {}
                return (strip_html(n.get("description") or ""), (n.get("datePosted") or "")[:10],
                        clean(org.get("name", "")) if isinstance(org, dict) else "")
    return "", "", ""


def fetch(config):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        seen, hits = set(), []
        for q in QUERIES:
            for page in range(1, MAX_PAGES + 1):
                try:
                    r = session.get(f"{BASE}/jobs", params={"k": q, "l": "United States", "p": page}, timeout=30)
                except Exception:
                    break
                if r.status_code != 200:
                    break
                cards = list(_cards(r.text))
                new = 0
                for c in cards:
                    m = re.search(r"id=(\d+)", c["url"])
                    key = m.group(1) if m else c["url"]
                    if key in seen:
                        continue
                    seen.add(key)
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
            cfg = dict(config, name=c["company"] or org or config.get("name", ""))
            try:
                out.append(make_job(cfg, "college_talent", c["title"], c["url"], c["loc"], desc, posted))
            except Exception:
                continue
        return out
    except Exception:
        return []
