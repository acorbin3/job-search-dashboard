"""SchoolSpring national keyword search (PowerSchool; K-12, postsecondary, adult ed).

The per-district `schoolspring` fetcher filters by domainName; without it the
same endpoint searches every board:
  GET https://api.schoolspring.com/api/Jobs/GetPagedJobsWithSearch?keyword=<q>&page=N&size=25
    -> value.jobsList[{jobId,title,employer,location,displayDate}]
  GET /api/Jobs/<jobId> -> value.jobInfo (jobDescription HTML, infoURL, postDate)
"""
import time

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "schoolspringnational"
needs_browser = False
API = "https://api.schoolspring.com/api/Jobs"
QUERIES = ["adjunct", "computer science", "computer", "technology instructor",
           "cybersecurity", "information technology", "programming"]
MAX_PAGES = 20
MAX_DETAILS = 200
DELAY = 1.0
_H = dict(HEADERS, Accept="application/json")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _detail(jid):
    r = requests.get(f"{API}/{jid}", headers=_H, timeout=20)
    if r.status_code != 200 or not r.text.lstrip().startswith("{"):
        return {}
    return ((r.json() or {}).get("value") or {}).get("jobInfo") or {}


def fetch(config):
    try:
        seen, hits = set(), []
        for q in QUERIES:
            for page in range(1, MAX_PAGES + 1):
                try:
                    r = requests.get(f"{API}/GetPagedJobsWithSearch",
                                     params={"keyword": q, "page": page, "size": 25, "sortDateAscending": "false"},
                                     headers=_H, timeout=25)
                except Exception:
                    break
                if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                    break
                jobs = ((r.json() or {}).get("value") or {}).get("jobsList") or []
                for j in jobs:
                    jid, title = j.get("jobId"), clean(j.get("title") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    if title_ok(title):
                        hits.append(j)
                time.sleep(DELAY)
                if len(jobs) < 25:
                    break
        out = []
        for i, j in enumerate(hits):
            info = {}
            if i < MAX_DETAILS:
                try:
                    info = _detail(j["jobId"])
                except Exception:
                    info = {}
                time.sleep(DELAY)
            href = info.get("infoURL") or f"https://www.schoolspring.com/job/{j['jobId']}"
            cfg = dict(config, name=clean(j.get("employer") or "") or config.get("name", ""))
            try:
                out.append(make_job(cfg, "college_schoolspringnational", clean(j["title"]), href,
                                    location=clean(j.get("location") or ""),
                                    description=strip_html(" ".join(info.get(k) or "" for k in ("jobDescription", "requirements")))[:20000],
                                    posted=(info.get("postDate") or j.get("displayDate") or "")[:10]))
            except Exception:
                continue
        return out
    except Exception:
        return []
