"""GovernmentJobs.com / SchoolJobs.com national keyword search (NeoGov, all agencies).

Complements the per-agency `neogov` fetcher: covers every community college and
school district on NeoGov, including ones not in data/colleges.json.

    GET /jobs?keyword=<q>&sort=date&isDescendingSort=True&page=N
        with X-Requested-With: XMLHttpRequest

returns an HTML fragment: #jobs-found ("668 jobs found. Page 1 ..."), one
li.job-item per posting with a.job-details-link (title/href /jobs/<id>-0/<slug>),
the employer in .job-organization (governmentjobs) or the first .primaryInfo
(schooljobs), .job-location, and a .description snippet. 10 per page.
Detail pages carry a schema.org JobPosting JSON-LD plus .term-block pairs
(Employer, Location, Opening Date, Department) and #details-info.
"""
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "governmentjobs"
needs_browser = False
HOSTS = ("https://www.governmentjobs.com", "https://www.schooljobs.com")
QUERIES = [
    "adjunct computer", "adjunct information technology", "adjunct cybersecurity",
    "instructor computer science", "lecturer computer", "faculty computer science",
    "adjunct data science", "instructor networking", "instructor programming",
]
MAX_PAGES = 20
MAX_DETAILS = 200
DELAY = 1.0
_LIST_HEADERS = {**HEADERS, "X-Requested-With": "XMLHttpRequest", "Accept": "text/html, */*; q=0.01"}
_ID_RE = re.compile(r"/jobs/(\d+)")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _page(session, host, q, page):
    r = session.get(f"{host}/jobs", params={"keyword": q, "sort": "date", "isDescendingSort": "True", "page": page},
                    headers=_LIST_HEADERS, timeout=30)
    if r.status_code != 200:
        return [], False
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select("li.job-item"):
        a = li.select_one("a.job-details-link")
        if not a or not a.get("href"):
            continue
        org = li.select_one(".job-organization")
        if not org:
            infos = [p for p in li.select(".primaryInfo") if "job-location" not in (p.get("class") or [])]
            org = infos[0] if infos else None
        loc = li.select_one(".job-location")
        snip = li.select_one(".description")
        items.append({"title": clean(a.get_text(" ", strip=True)), "url": urljoin(host, a["href"]),
                      "org": clean(org.get_text(" ", strip=True)) if org else "",
                      "loc": clean(loc.get_text(" ", strip=True)) if loc else "",
                      "snippet": clean(snip.get_text(" ", strip=True)) if snip else ""})
    has_next = bool(re.search(rf"[?&]page={page + 1}\b", r.text))
    return items, has_next


def _detail(session, url):
    """(description, posted, employer, location) from JSON-LD and the term blocks."""
    r = session.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return "", "", "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    desc, posted, emp, loc = "", "", "", ""
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text())
        except Exception:
            continue
        if isinstance(j, dict) and j.get("@type") == "JobPosting":
            desc = strip_html(j.get("description") or "")
            posted = (j.get("datePosted") or "")[:10]
            org = j.get("hiringOrganization") or {}
            emp = clean(org.get("name", "")) if isinstance(org, dict) else ""
            break
    fields = {}
    for blk in soup.select(".term-block"):
        lbl, val = blk.select_one(".term-description"), blk.select_one(".span8")
        if lbl and val:
            fields[clean(lbl.get_text(" ", strip=True)).lower().rstrip(":")] = clean(val.get_text(" ", strip=True))
    emp = emp or fields.get("employer", "")
    loc = fields.get("location", "")
    if not posted:
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", fields.get("opening date", ""))
        if m:
            posted = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    if not desc:
        info = soup.find(id="details-info")
        desc = strip_html(str(info)) if info else ""
    meta = "\n".join(f"{k.title()}: {v}" for k, v in fields.items() if v)
    return f"{meta}\n\n{desc}".strip(), posted, emp, loc


def fetch(config):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        seen, hits = set(), []
        for host in HOSTS:
            for q in QUERIES:
                for page in range(1, MAX_PAGES + 1):
                    try:
                        items, has_next = _page(session, host, q, page)
                    except Exception:
                        break
                    new = 0
                    for it in items:
                        m = _ID_RE.search(it["url"])
                        key = m.group(1) if m else it["url"]
                        if key in seen:
                            continue
                        seen.add(key)
                        new += 1
                        if title_ok(it["title"]):
                            hits.append(it)
                    time.sleep(DELAY)
                    if not items or not has_next or new == 0:
                        break
        out = []
        for i, it in enumerate(hits):
            desc, posted, emp, loc = "", "", "", ""
            if i < MAX_DETAILS:
                try:
                    desc, posted, emp, loc = _detail(session, it["url"])
                except Exception:
                    pass
                time.sleep(DELAY)
            cfg = dict(config, name=emp or it["org"] or config.get("name", ""))
            try:
                out.append(make_job(cfg, "college_governmentjobs", it["title"], it["url"],
                                    loc or it["loc"], desc or it["snippet"], posted))
            except Exception:
                continue
        return out
    except Exception:
        return []
