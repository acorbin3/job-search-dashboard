"""CommunityCollegeJobs.com (CCJobs): https://www.communitycollegejobs.com

Plain PHP board, no bot wall. Search is
`/search.php?s=<kw>&per_page=50&sort=newest&page=<n>`; each result is a
div.row with h5.jobtitle a (title + relative "jobdetail-<id>-<slug>" link),
two p.joblocation lines (institution, "City, State") and p.jobposted
("Posted : Month D, YYYY"). Detail pages have the text in div.jobd-content and
a JobPosting JSON-LD whose description field is junk (just the college name),
so we take the HTML block. No RSS.
"""
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "ccjobs"
needs_browser = False
BASE = "https://www.communitycollegejobs.com"
QUERIES = ["adjunct computer", "adjunct information technology", "adjunct cybersecurity", "adjunct software",
           "adjunct data science", "adjunct programming", "instructor computer science",
           "instructor information technology", "instructor cybersecurity", "computer science faculty",
           "part-time computer", "online computer"]
PER_PAGE = 50
MAX_PAGES = 3
MAX_DETAILS = 150
DELAY = 1.0
POSTED_RE = re.compile(r"Posted\s*:?\s*([A-Za-z]+ \d{1,2}, \d{4})")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _date(s):
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return time.strftime("%Y-%m-%d", time.strptime(s.strip(), fmt))
        except Exception:
            pass
    return ""


def _rows(html):
    soup = BeautifulSoup(html, "html.parser")
    for h5 in soup.select("h5.jobtitle"):
        a = h5.find("a", href=True)
        if not a:
            continue
        box = h5.parent
        locs = [clean(p.get_text(" ", strip=True)) for p in box.select("p.joblocation")]
        row = h5.find_parent("div", class_="row") or box
        m = POSTED_RE.search(row.get_text(" ", strip=True))
        yield {
            "title": clean(a.get_text(" ", strip=True)),
            "url": urljoin(BASE + "/", a["href"]),
            "institution": locs[0] if locs else "",
            "location": locs[1] if len(locs) > 1 else "",
            "posted": _date(m.group(1)) if m else "",
        }


def _detail(session, url):
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    box = soup.select_one("div.jobd-content") or soup.find("main")
    return strip_html(str(box)) if box else ""


def fetch(config):
    out, seen, hits = [], set(), []
    session = requests.Session()
    session.headers.update(HEADERS)
    for q in QUERIES:
        for page in range(1, MAX_PAGES + 1):
            try:
                r = session.get(f"{BASE}/search.php", params={"s": q, "per_page": PER_PAGE, "sort": "newest", "page": page}, timeout=30)
            except Exception:
                break
            if r.status_code != 200:
                break
            rows, new = list(_rows(r.text)), 0
            for row in rows:
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                new += 1
                if title_ok(row["title"]):
                    hits.append(row)
            time.sleep(DELAY)
            if new == 0 or len(rows) < PER_PAGE:
                break
    for i, row in enumerate(hits):
        desc = ""
        if i < MAX_DETAILS:
            try:
                desc = _detail(session, row["url"])
            except Exception:
                desc = ""
            time.sleep(DELAY)
        cfg = dict(config)
        if row["institution"]:
            cfg["name"] = row["institution"]
        try:
            out.append(make_job(cfg, "college_ccjobs", row["title"], row["url"], row["location"], desc, row["posted"]))
        except Exception:
            continue
    return out
