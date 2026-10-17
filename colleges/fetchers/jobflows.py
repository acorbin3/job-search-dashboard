"""JobFlows (monday.com job boards): https://go.jobflows.co/<slug>

config: {"url": "https://go.jobflows.co/<slug>", "jobflows_slug": "<slug>"}
GET https://jobflows.jtpk.app/jobs/searchAndFilter/<slug> -> {"jobs": [...],
"JobsNumber": N}; every job carries _id, name, location, Department,
Description (HTML), IsRemote, salary, Position_Open_Date / createdAt. The
posting page is https://go.jobflows.co/<slug>/<_id>.
Verified against Luther College (10 jobs).
"""
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "jobflows"
needs_browser = False
SLUG_RE = re.compile(r"https?://go\.jobflows\.co/([A-Za-z0-9_-]+)")
API = "https://jobflows.jtpk.app/jobs/searchAndFilter/"


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() in ("_next", "api", "favicon.ico"):
        return None
    return {"url": f"https://go.jobflows.co/{m.group(1)}", "jobflows_slug": m.group(1)}


def fetch(config):
    try:
        slug = config.get("jobflows_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        r = requests.get(API + slug, headers=dict(HEADERS, Accept="application/json"), timeout=30)
        if r.status_code != 200:
            return []
        out, seen = [], set()
        for j in (r.json() or {}).get("jobs") or []:
            jid = str(j.get("_id") or "")
            title = clean(j.get("name") or "")
            if not jid or jid in seen or not title_ok(title):
                continue
            seen.add(jid)
            loc = clean(j.get("location") or "")
            if str(j.get("IsRemote") or "").lower() in ("yes", "true") and "remote" not in loc.lower():
                loc = f"{loc} (Remote)".strip()
            body = j.get("Description") or ""
            if j.get("salary"):
                body = f"Salary: {j['salary']}\n{body}"
            out.append(make_job(config, "college_jobflows", title, f"https://go.jobflows.co/{slug}/{jid}",
                                location=loc, description=strip_html(body)[:20000],
                                posted=str(j.get("Position_Open_Date") or j.get("createdAt") or "")[:10],
                                department=clean(j.get("Department") or "")))
        return out
    except Exception:
        return []
