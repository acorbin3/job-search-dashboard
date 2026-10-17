"""AcademicKeys aggregator: https://www.academickeys.com

Jobs live on per-discipline subsites (communitycolleges., engineering.,
sciences. ...academickeys.com). Each has a plain GET search
`/seeker_search.php?q=<kw>&start=<n>` (20 rows/page, no bot wall). Rows are
<tr> blocks: first <strong> holds the title link (/job/<id>/<slug>), the
second the institution, then a department line, a div.small with
"City, State<br>Country" and a "Date Posted Mon. D, YYYY" cell. Detail pages
hold the posting text in #job_descrip__default (no JSON-LD). /rss per subsite
exists but is a short unfiltered recent-jobs feed, so we use the search.
"""
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "academickeys"
needs_browser = False

# subsite -> keyword queries (community colleges carry most adjunct postings;
# the engineering/sciences sites hold CS departments at universities)
PLAN = {
    "communitycolleges": ["adjunct computer", "adjunct information technology", "adjunct cybersecurity",
                          "adjunct software", "adjunct data", "instructor computer science",
                          "instructor information technology", "instructor cybersecurity",
                          "computer science", "information systems"],
    "engineering": ["adjunct computer", "lecturer computer science", "instructor computer science",
                    "adjunct cybersecurity", "adjunct software", "computer science online"],
    "sciences": ["adjunct computer", "lecturer computer science", "instructor computer science",
                 "adjunct data science", "adjunct cybersecurity"],
}
PAGE = 20
MAX_PAGES = 3
MAX_DETAILS = 150
DELAY = 1.0
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(r"Date Posted\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _date(text):
    m = DATE_RE.search(text or "")
    if not m or m.group(1).lower()[:3] not in MONTHS:
        return ""
    return f"{m.group(3)}-{MONTHS[m.group(1).lower()[:3]]:02d}-{int(m.group(2)):02d}"


def _rows(html, base):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("tr a[href*='/job/']"):
        tr = a.find_parent("tr")
        if not tr or not a.get_text(strip=True):
            continue
        strongs = tr.find_all("strong")
        inst = clean(strongs[1].get_text(" ", strip=True)) if len(strongs) > 1 else ""
        loc_div = tr.select_one("ul.form div.small")
        loc, country = "", ""
        if loc_div:
            parts = [clean(x) for x in loc_div.get_text("\n").split("\n") if clean(x)]
            if parts:
                loc, country = parts[0], (parts[-1] if len(parts) > 1 else "")
        yield {
            "title": clean(a.get_text(" ", strip=True)),
            "url": urljoin(base, a["href"]).split("?")[0],
            "institution": inst, "location": loc, "country": country,
            "posted": _date(tr.get_text(" ", strip=True)),
        }


def _detail(session, url):
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    box = soup.select_one("#job_descrip__default") or soup.select_one("table.result")
    return strip_html(str(box)) if box else ""


def fetch(config):
    out, seen, hits = [], set(), []
    session = requests.Session()
    session.headers.update(HEADERS)
    for sub, queries in PLAN.items():
        base = f"https://{sub}.academickeys.com"
        for q in queries:
            for page in range(MAX_PAGES):
                try:
                    r = session.get(f"{base}/seeker_search.php", params={"q": q, "start": page * PAGE}, timeout=30)
                except Exception:
                    break
                if r.status_code != 200:
                    break
                new = 0
                for row in _rows(r.text, base):
                    if row["url"] in seen:
                        continue
                    seen.add(row["url"])
                    new += 1
                    if row["country"] and "united states" not in row["country"].lower():
                        continue
                    if title_ok(row["title"]):
                        hits.append(row)
                time.sleep(DELAY)
                if new < PAGE:
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
            out.append(make_job(cfg, "college_academickeys", row["title"], row["url"], row["location"], desc, row["posted"]))
        except Exception:
            continue
    return out
