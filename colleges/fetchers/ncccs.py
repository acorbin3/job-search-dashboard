"""North Carolina Community College System job listings:
https://www.nccommunitycolleges.edu/about-us/jobs/

The System Office runs one WordPress job board that all 58 colleges post
into, served through a Next.js front end. The listing page embeds the whole
feed server-side in <script id="__NEXT_DATA__">:

    props.pageProps.__TEMPLATE_QUERY_DATA__.jobListings.nodes[]   (first: 500)
        title, uri, jobListing.{jobTitle, colleges[].collegeDetails.name,
                                jobCloseDate "mm/dd/yyyy", jobCategory[],
                                employmentType}

Detail pages (/about-us/jobs/<slug>/) embed the same way with
__TEMPLATE_QUERY_DATA__.jobListing.content (HTML body) plus the fields above.
Plain requests work; one request per detail, bounded by MAX_DETAILS.
"""
import html as html_mod
import json
import re
import time
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "ncccs"
needs_browser = False
BASE = "https://www.nccommunitycolleges.edu"
LIST = BASE + "/about-us/jobs/"
MAX_DETAILS = 120
DELAY = 0.7
_ND = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _next_data(html):
    m = _ND.search(html)
    if not m:
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__NEXT_DATA__")
        if not sc:
            return {}
        raw = sc.get_text()
    else:
        raw = m.group(1)
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _tq(j):
    return ((j.get("props") or {}).get("pageProps") or {}).get("__TEMPLATE_QUERY_DATA__") or {}


def _date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(clean(s), fmt).date()
        except ValueError:
            continue
    return None


def _nodes(html):
    j = _next_data(html)
    nodes = ((_tq(j).get("jobListings") or {}).get("nodes")) or []
    out = []
    for n in nodes:
        jl = n.get("jobListing") or {}
        cols = [((c.get("collegeDetails") or {}).get("name") or c.get("title") or "")
                for c in (jl.get("colleges") or []) if isinstance(c, dict)]
        cat = jl.get("jobCategory") or []
        out.append({
            "title": clean(html_mod.unescape(jl.get("jobTitle") or n.get("title") or "")),
            "url": urljoin(BASE, n.get("uri") or ""),
            "college": clean(cols[0]) if cols else "",
            "close": jl.get("jobCloseDate") or "",
            "category": cat[-1] if isinstance(cat, list) and cat else (cat if isinstance(cat, str) else ""),
            "type": jl.get("employmentType") or "",
        })
    return out


def _detail(session, url):
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException:
        return "", ""
    time.sleep(DELAY)
    if r.status_code != 200:
        return "", ""
    jl = _tq(_next_data(r.text)).get("jobListing") or {}
    content = jl.get("content") or ""
    extra = jl.get("jobListing") or {}
    lines = [f"{k}: {v}" for k, v in (("Salary", extra.get("salary")), ("Application", extra.get("applicationInstructions")),
                                      ("Posted", extra.get("jobPostDate") or extra.get("datePosted"))) if v]
    posted = ""
    for k in ("jobPostDate", "datePosted", "postDate"):
        d = _date(str(extra.get(k) or ""))
        if d:
            posted = d.isoformat()
            break
    if not content and not lines:
        soup = BeautifulSoup(r.text, "html.parser")
        main = soup.find("main") or soup.body
        content = str(main) if main else ""
    return ("\n".join(lines) + "\n\n" + strip_html(content)).strip(), posted


def fetch(config):
    out, seen, details = [], set(), 0
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        r = session.get(LIST, timeout=60)
        if r.status_code != 200:
            return out
        rows = _nodes(r.text)
    except Exception:
        return out
    for row in rows:
        if not row["title"] or row["url"] in seen or not title_ok(row["title"]):
            continue
        d = _date(row["close"])
        if d and d < date.today():
            continue
        seen.add(row["url"])
        desc, posted = "", ""
        if details < MAX_DETAILS:
            details += 1
            try:
                desc, posted = _detail(session, row["url"])
            except Exception:
                pass
        header = (f"{row['title']}\nCollege: {row['college']}\nEmployment Type: {row['type']}\n"
                  f"Category: {row['category']}\nClosing Date: {row['close']}\n\n")
        loc = f"{row['college']}, NC" if row["college"] else "NC"
        try:
            job = make_job(config, "college_ncccs", row["title"], row["url"], loc, header + desc, posted)
        except Exception:
            continue
        job.company = row["college"] or job.company
        out.append(job)
    return out
