"""USAJobs.gov public search (federal academies, DoD schools, CCAF, DLI, NCU ...).

data.usajobs.gov needs an API key, but the website's own search backend does
not: the results page JS POSTs JSON to /Search/ExecuteSearch (no verification
token required) and gets back {"Total", "Pager": {NumberOfPages, ...},
"Jobs": [{Title, Agency, Department, LocationDisplay, PositionURI,
PositionStartDate, SalaryDisplay, ...}]}. Detail pages (/job/<id>) carry a
JobPosting JSON-LD plus #joa-summary / #joa-duties / #joa-requirements.
"""
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "usajobs"
needs_browser = False
BASE = "https://www.usajobs.gov"
QUERIES = [
    "instructor computer", "instructor cyber", "instructor information technology",
    "professor computer science", "faculty cyber", "lecturer computer",
    "professor", "computer science", "cybersecurity instructor", "assistant professor",
]
MAX_PAGES = 10
MAX_DETAILS = 100
DELAY = 1.0


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _search(session, q, page):
    r = session.post(f"{BASE}/Search/ExecuteSearch",
                     json={"Keyword": q, "Page": str(page), "SortField": "", "SortDirection": ""},
                     headers={**HEADERS, "Content-Type": "application/json; charset=utf-8",
                              "Accept": "application/json", "Referer": f"{BASE}/Search/Results?k={q}"},
                     timeout=30)
    if r.status_code != 200 or not r.text.lstrip().startswith("{"):
        return [], 0
    j = r.json()
    pages = int((j.get("Pager") or {}).get("NumberOfPages") or 0)
    return j.get("Jobs") or [], pages


def _detail(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    desc, posted = "", ""
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text())
        except Exception:
            continue
        if isinstance(j, dict) and j.get("@type") == "JobPosting":
            desc = strip_html(j.get("description") or "")
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", j.get("datePosted") or "")
            if m:
                posted = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            break
    parts = []
    for sid in ("joa-summary", "joa-duties", "joa-requirements"):
        el = soup.find(id=sid)
        if el:
            parts.append(strip_html(str(el)))
    body = "\n\n".join(parts)
    return (body if len(body) > len(desc) else desc), posted


def fetch(config):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        seen, hits = set(), []
        for q in QUERIES:
            page, pages = 1, 1
            while page <= min(pages, MAX_PAGES):
                try:
                    jobs, pages = _search(session, q, page)
                except Exception:
                    break
                if not jobs:
                    break
                for j in jobs:
                    url = (j.get("PositionURI") or "").replace(":443", "")
                    if not url:
                        url = f"{BASE}/job/{j.get('DocumentID', '')}"
                    if url in seen:
                        continue
                    seen.add(url)
                    title = clean(j.get("Title") or "")
                    if title_ok(title):
                        hits.append((title, url, j))
                page += 1
                time.sleep(DELAY)
        out = []
        for i, (title, url, j) in enumerate(hits):
            desc, posted = "", ""
            if i < MAX_DETAILS:
                try:
                    desc, posted = _detail(session, url)
                except Exception:
                    pass
                time.sleep(DELAY)
            posted = posted or (j.get("PositionStartDate") or "")[:10]
            agency = clean(j.get("Agency") or j.get("SubAgency") or j.get("Department") or "")
            meta = "\n".join(f"{k}: {j.get(f)}" for k, f in (("Department", "Department"), ("Agency", "Agency"),
                                                               ("Sub-agency", "SubAgency"), ("Salary", "SalaryDisplay"),
                                                               ("Schedule", "WorkSchedule"), ("Open", "DateDisplay")) if j.get(f))
            cfg = dict(config, name=agency or config.get("name", ""))
            try:
                out.append(make_job(cfg, "college_usajobs", title, url,
                                    clean(j.get("LocationDisplay") or j.get("Location") or ""),
                                    f"{meta}\n\n{desc}".strip(), posted,
                                    clean(j.get("Department") or "")))
            except Exception:
                continue
        return out
    except Exception:
        return []
