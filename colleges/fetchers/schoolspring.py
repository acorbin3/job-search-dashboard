"""PowerSchool Unified Talent / TalentEd Hire boards (tedk12.com), which now
redirect to <district>.schoolspring.com, a JS app over api.schoolspring.com:
  GET /api/Jobs/GetPagedJobsWithSearch?domainName=<district>.schoolspring.com&page=N&size=25
    -> value.jobsList[{jobId,title,employer,location,displayDate}]
  GET /api/Jobs/<jobId>?domainName=... -> value.jobInfo (jobDescription HTML,
    infoURL back on tedk12.com, payMin/payMax, postDate).

config: {"url": ..., "domain": "<district>.schoolspring.com"}
Verified against Canadian Valley Tech (cvtech; also Autry, Mitchell Tech).
"""
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "schoolspring"
needs_browser = False
API = "https://api.schoolspring.com/api/Jobs"
MAX_PAGES = 40

HOST_RE = re.compile(r"https?://([a-z0-9-]+)\.(?:tedk12|schoolspring)\.com", re.I)


def detect(url, html=""):
    m = HOST_RE.search(url or "") or HOST_RE.search(html or "")
    if not m or m.group(1).lower() in ("www", "api", "app"):
        return None
    dom = f"{m.group(1).lower()}.schoolspring.com"
    return {"url": f"https://{dom}/", "domain": dom}


def _detail(jid, dom):
    try:
        r = requests.get(f"{API}/{jid}", params={"domainName": dom},
                         headers=dict(HEADERS, Accept="application/json"), timeout=20)
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            return {}
        return ((r.json() or {}).get("value") or {}).get("jobInfo") or {}
    except Exception:
        return {}


def fetch(config):
    try:
        dom = config.get("domain") or (HOST_RE.search(config["url"]) or [None, ""])[1]
        if "." not in dom:
            dom = f"{dom}.schoolspring.com"
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            r = requests.get(f"{API}/GetPagedJobsWithSearch",
                             params={"domainName": dom, "page": page, "size": 25, "sortDateAscending": "false"},
                             headers=dict(HEADERS, Accept="application/json"), timeout=25)
            if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                break
            jobs = ((r.json() or {}).get("value") or {}).get("jobsList") or []
            for j in jobs:
                title, jid = clean(j.get("title") or ""), j.get("jobId")
                if not title or not jid or jid in seen or not title_ok(title):
                    continue
                seen.add(jid)
                info = _detail(jid, dom)
                href = info.get("infoURL") or f"https://{dom}/job/{jid}"
                out.append(make_job(config, "college_schoolspring", title, href,
                                    location=clean(j.get("location") or ""),
                                    description=strip_html(" ".join(info.get(k) or "" for k in ("jobDescription", "requirements")))[:20000],
                                    posted=(info.get("postDate") or j.get("displayDate") or "")[:10],
                                    department=clean(j.get("employer") or "")))
            if len(jobs) < 25:
                break
        return out
    except Exception:
        return []
