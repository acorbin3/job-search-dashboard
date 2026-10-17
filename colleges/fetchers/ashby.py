"""Ashby job boards: https://jobs.ashbyhq.com/<slug>

config: {"url": "https://jobs.ashbyhq.com/<slug>", "ashby_slug": "<slug>"}
The public posting API https://api.ashbyhq.com/posting-api/job-board/<slug>
returns every listed job (title, location, jobUrl, descriptionPlain,
publishedAt, isRemote); no detail page needed.
Verified against Multiverse and Instructure.
"""
import re

import requests

from ._common import HEADERS, make_job, title_ok, clean

name = "ashby"
needs_browser = False

SLUG_RE = re.compile(r"https?://jobs\.ashbyhq\.com/([a-z0-9][a-z0-9._-]*)", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() in ("api", "embed"):
        return None
    slug = m.group(1)
    return {"url": f"https://jobs.ashbyhq.com/{slug}", "ashby_slug": slug}


def fetch(config):
    try:
        slug = config.get("ashby_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         headers=dict(HEADERS, Accept="application/json"), timeout=25)
        if r.status_code != 200:
            return []
        out, seen = [], set()
        for j in r.json().get("jobs") or []:
            title = clean(j.get("title") or "")
            href = j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id', '')}"
            if not title or href in seen or not title_ok(title) or j.get("isListed") is False:
                continue
            seen.add(href)
            loc = clean(j.get("location") or "")
            if j.get("isRemote") and "remote" not in loc.lower():
                loc = f"{loc} (Remote)".strip()
            out.append(make_job(config, "college_ashby", title, href, location=loc,
                                description=(j.get("descriptionPlain") or "")[:20000],
                                posted=(j.get("publishedAt") or "")[:10],
                                department=clean(j.get("department") or "")))
        return out
    except Exception:
        return []
