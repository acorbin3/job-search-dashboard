"""SUNY system-wide employment search: https://www.suny.edu/careers/employment/

ColdFusion page that aggregates every SUNY campus's openings (64 campuses:
university centers, colleges, community colleges). Plain requests work.

    POST /careers/employment/index.cfm?s=y   filter=<keyword>&campus=-1
        #numOfJobs h2 "# of Positions Available: N"
        #showJobsHere > a[href] (link straight to the campus ATS posting)
            div.jobListing[data-campID]  .jobTitle / .campusLogo (campus name)
                                          .jobCatagory ("Region: ...")
                                          .jobEndDate ("Open until filled" / "End Date: m/d/yy")

The keyword filter is a substring match over titles, so we run a handful of
computing keywords and de-duplicate on the link. Links point at ~30 different
campus boards (Interview Exchange, PeopleAdmin, PageUp, Upstate's own site...).
Interview Exchange detail pages sit behind Cloudflare, so details are fetched
only where a plain GET returns 200 (PeopleAdmin / PageUp / campus sites); the
rest carry the listing's own fields as the description.
"""
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "suny"
needs_browser = False
BASE = "https://www.suny.edu/careers/employment/"
QUERIES = ["computer", "information", "cyber", "software", "data", "network",
           "programming", "informatics", "artificial", "technology"]
MAX_DETAILS = 120
DELAY = 0.8
_NO_DETAIL = re.compile(r"interviewexchange\.com", re.I)   # Cloudflare JS wall


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _rows(html):
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("#showJobsHere") or soup
    for a in box.select("a[href]"):
        card = a.select_one("div.jobListing")
        if not card:
            continue
        t = card.select_one(".jobTitle")
        campus = card.select_one(".campusLogo")
        region = card.select_one(".jobCatagory")
        end = card.select_one(".jobEndDate")
        yield {
            "title": clean(t.get_text(" ", strip=True)) if t else "",
            "url": urljoin(BASE, a["href"].strip()),
            "campus": clean(campus.get_text(" ", strip=True)) if campus else "",
            "region": clean(region.get_text(" ", strip=True)).replace("Region:", "").strip() if region else "",
            "end": clean(end.get_text(" ", strip=True)) if end else "",
        }


def _detail(session, url):
    if _NO_DETAIL.search(url):
        return ""
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
    except requests.RequestException:
        return ""
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for sel in ("#job-content", ".job-details", "#content", "main", "article"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 200:
            return strip_html(str(el))
    return strip_html(str(soup.body)) if soup.body else ""


def fetch(config):
    out, seen, hits = [], set(), []
    session = requests.Session()
    session.headers.update(HEADERS)
    for q in QUERIES:
        try:
            r = session.post(BASE + "index.cfm?s=y", data={"filter": q, "campus": "-1"}, timeout=40)
        except requests.RequestException:
            continue
        time.sleep(DELAY)
        if r.status_code != 200:
            continue
        for row in _rows(r.text):
            if not row["title"] or row["url"] in seen:
                continue
            seen.add(row["url"])
            if title_ok(row["title"]):
                hits.append(row)
    for i, row in enumerate(hits):
        desc = ""
        if i < MAX_DETAILS:
            try:
                desc = _detail(session, row["url"])
            except Exception:
                desc = ""
            time.sleep(DELAY)
        header = (f"{row['title']}\nCampus: SUNY {row['campus']}\nRegion: {row['region']}\n"
                  f"Closing: {row['end']}\n\n")
        loc = f"{row['campus']}, NY" if row["campus"] else "NY"
        try:
            job = make_job(config, "college_suny", row["title"], row["url"], loc, header + desc, "")
        except Exception:
            continue
        job.company = f"SUNY {row['campus']}" if row["campus"] else job.company
        out.append(job)
    return out
