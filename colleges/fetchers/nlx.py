"""NLx / DirectEmployers career sites (the .jobs / .careers Nuxt front ends,
e.g. https://cuny.jobs/jobs/, https://uagc.careers/jobs/).

config: {"url": "https://<host>/jobs/", "origin": "<host>"}
The page is an empty Nuxt shell (markers: seo.nlx.org assets, window.__NUXT__
config with "x-origin"); the app calls
GET https://prod-search-api.jobsyn.org/api/v1/solr/search?page=N&num_items=10
with the header x-origin: <host> (400 without it) -> jobs[{guid, title_exact,
title_slug, location_exact, location_name, description (full text), date_new,
job_type}], pagination{has_more_pages, total_pages}; 10 per page whatever
num_items says. The posting page is https://<host>/<title_slug>/<guid>/job/.
Not Radancy: the TalentBrew-style /search-jobs/results JSON does not exist here.
Verified against CUNY (273 jobs) and University of Arizona Global Campus.
"""
import re
from urllib.parse import urlsplit

import requests

from ._common import HEADERS, make_job, title_ok, clean

name = "nlx"
needs_browser = False
API = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
MARKERS = ("seo.nlx.org", "jobsyn.org", '"x-origin"', "x-origin:")
MAX_PAGES = 100


def detect(url, html=""):
    h = html or ""
    p = urlsplit(url or "")
    if not p.netloc or not any(m in h for m in MARKERS):
        return None
    host = p.netloc.lower()
    return {"url": f"https://{host}/jobs/", "origin": host}


def fetch(config):
    try:
        origin = config.get("origin") or urlsplit(config["url"]).netloc.lower()
        hdrs = dict(HEADERS, Accept="application/json", **{"x-origin": origin})
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            r = requests.get(API, params={"page": page, "num_items": 10}, headers=hdrs, timeout=30)
            if r.status_code != 200:
                break
            d = r.json() or {}
            for j in d.get("jobs") or []:
                guid = str(j.get("guid") or "")
                title = clean(j.get("title_exact") or j.get("title") or "")
                if not guid or guid in seen or not title_ok(title):
                    continue
                seen.add(guid)
                body = j.get("description") or ""
                if j.get("job_type"):
                    body = f"Job type: {j['job_type']}\n{body}"
                out.append(make_job(config, "college_nlx", title, f"https://{origin}/{j.get('title_slug') or 'job'}/{guid}/job/",
                                    location=clean(j.get("location_exact") or ""), description=body[:20000],
                                    posted=str(j.get("date_new") or j.get("date_added") or "")[:10],
                                    department=clean(j.get("location_name") or "")))
            if not (d.get("pagination") or {}).get("has_more_pages"):
                break
        return out
    except Exception:
        return []
