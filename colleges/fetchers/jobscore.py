"""JobScore boards: https://careers.jobscore.com/careers/<slug>

config: {"url": ..., "jobscore_slug": "<slug>"}
GET /careers/<slug>/feed returns JSON {"jobs": [...]} with title, detail_url,
location, department, opened_date and the full HTML description, so no
detail fetch is needed.
Verified against Roanoke College.
"""
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "jobscore"
needs_browser = False

SLUG_RE = re.compile(r"https?://careers\.jobscore\.com/careers/([a-z0-9_-]+)", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m:
        return None
    slug = m.group(1)
    return {"url": f"https://careers.jobscore.com/careers/{slug}", "jobscore_slug": slug}


def fetch(config):
    try:
        slug = config.get("jobscore_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        r = requests.get(f"https://careers.jobscore.com/careers/{slug}/feed",
                         headers=dict(HEADERS, Accept="application/json"), timeout=25)
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            return []
        out, seen = [], set()
        for j in (r.json() or {}).get("jobs") or []:
            title = clean(j.get("title") or "")
            href = (j.get("detail_url") or "").split("?")[0]
            if not title or not href or href in seen or not title_ok(title):
                continue
            seen.add(href)
            out.append(make_job(config, "college_jobscore", title, href,
                                location=clean(j.get("location") or ""),
                                description=strip_html(j.get("description") or "")[:20000],
                                posted=(j.get("opened_date") or "")[:10],
                                department=clean(j.get("department") or "")))
        return out
    except Exception:
        return []
