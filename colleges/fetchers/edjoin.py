"""EdJoin (California K-12 districts, county offices and community colleges).

/Home/Jobs?keywords=<q> is a jQuery page that calls

  GET /Home/LoadJobs?rows=50&page=N&sort=postingDate&order=desc&keywords=<q>&... (XHR)
    -> {"totalPages", "totalRecords", "data": [{postingID, positionTitle,
        districtName, city, countyName, postingDate "/Date(ms)/", salaryInfo}]}

(every filter must be passed, as 0 or "", or the server 500s). Detail pages
/Home/JobPosting/<postingID> embed a JobPosting JSON-LD with the description.
"""
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "edjoin"
needs_browser = False
BASE = "https://www.edjoin.org"
QUERIES = ["adjunct", "computer science", "instructor computer", "cybersecurity",
           "information technology", "computer information systems"]
MAX_PAGES = 10
MAX_DETAILS = 150
DELAY = 1.0
_XH = {**HEADERS, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _params(q, page):
    return {"rows": 50, "page": page, "sort": "postingDate", "sortVal": 2, "order": "desc",
            "keywords": q, "location": "", "searchType": "all", "regions": "", "jobTypes": "",
            "days": 0, "empType": "", "catID": 0, "onlineApps": "", "recruitmentCenterID": 0,
            "stateID": 0, "regionID": 0, "districtID": 0, "searchID": 0}


def _date(s):
    m = re.search(r"\d{10,}", s or "")
    if not m:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(m.group()) / 1000))
    except Exception:
        return ""


def _detail(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text())
        except Exception:
            continue
        if isinstance(j, dict) and j.get("@type") == "JobPosting" and j.get("description"):
            return strip_html(j["description"])
    body = soup.select_one(".jobPostingbody")
    return strip_html(str(body)) if body else ""


def fetch(config):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get(f"{BASE}/Home/Jobs", params={"keywords": QUERIES[0]}, timeout=30)  # session cookie
        seen, hits = set(), []
        for q in QUERIES:
            for page in range(1, MAX_PAGES + 1):
                try:
                    r = session.get(f"{BASE}/Home/LoadJobs", params=_params(q, page),
                                    headers={**_XH, "Referer": f"{BASE}/Home/Jobs?keywords={q}"}, timeout=30)
                except Exception:
                    break
                if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                    break
                j = r.json()
                rows = j.get("data") or []
                for it in rows:
                    pid = it.get("postingID")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    if title_ok(clean(it.get("positionTitle") or "")):
                        hits.append(it)
                time.sleep(DELAY)
                if page >= int(j.get("totalPages") or 0) or not rows:
                    break
        out = []
        for i, it in enumerate(hits):
            url = f"{BASE}/Home/JobPosting/{it['postingID']}"
            desc = ""
            if i < MAX_DETAILS:
                try:
                    desc = _detail(session, url)
                except Exception:
                    pass
                time.sleep(DELAY)
            loc = ", ".join(x for x in (clean(it.get("city") or ""), "CA") if x)
            meta = "\n".join(f"{k}: {v}" for k, v in (("District", it.get("districtName")), ("County", it.get("countyName")),
                                                       ("Salary", it.get("salaryInfo"))) if v)
            cfg = dict(config, name=clean(it.get("districtName") or "") or config.get("name", ""))
            try:
                out.append(make_job(cfg, "college_edjoin", clean(it["positionTitle"]), url, loc,
                                    f"{meta}\n\n{desc}".strip(), _date(it.get("postingDate"))))
            except Exception:
                continue
        return out
    except Exception:
        return []
