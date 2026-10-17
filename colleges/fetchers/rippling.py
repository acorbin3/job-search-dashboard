"""Rippling ATS boards: https://ats.rippling.com/<slug>/jobs

config: {"url": ..., "rippling_slug": "<slug>"}
The Next.js page embeds the openings in __NEXT_DATA__ (items: id, name, url,
department.name, locations[].name); the job page's __NEXT_DATA__ carries
apiData.jobPost.description as a dict of HTML sections.
Verified against Bethel University (faculty board).
"""
import json
import re

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "rippling"
needs_browser = False

SLUG_RE = re.compile(r"https?://ats\.rippling\.com/([a-z0-9_-]+)", re.I)
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m:
        return None
    return {"url": f"https://ats.rippling.com/{m.group(1)}/jobs", "rippling_slug": m.group(1)}


def _next_data(html):
    m = NEXT_RE.search(html or "")
    try:
        return json.loads(m.group(1)) if m else {}
    except Exception:
        return {}


def _find_items(o):
    """Depth-first search for the postings list inside dehydratedState."""
    if isinstance(o, dict):
        v = o.get("items")
        if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0] and "url" in v[0]:
            return v
        for v in o.values():
            r = _find_items(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_items(v)
            if r:
                return r
    return None


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        jp = ((_next_data(r.text).get("props") or {}).get("pageProps") or {}).get("apiData", {}).get("jobPost") or {}
        desc = jp.get("description")
        if isinstance(desc, dict):
            desc = " ".join(str(v) for v in desc.values() if v)
        if not desc:
            box = BeautifulSoup(r.text, "html.parser").find("main")
            desc = str(box) if box else ""
        return strip_html(str(desc))[:20000]
    except Exception:
        return ""


def fetch(config):
    try:
        slug = config.get("rippling_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        url = f"https://ats.rippling.com/{slug}/jobs"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        items = _find_items((_next_data(r.text).get("props") or {}).get("pageProps") or {}) or []
        out, seen = [], set()
        if not items:   # fall back to the anchors
            soup = BeautifulSoup(r.text, "html.parser")
            items = [{"name": a.get_text(" ", strip=True), "url": a["href"]}
                     for a in soup.find_all("a", href=re.compile(rf"/{re.escape(slug)}/jobs/[0-9a-f-]{{36}}"))]
        for j in items:
            title = clean(j.get("name") or "")
            href = j.get("url") or ""
            if href.startswith("/"):
                href = "https://ats.rippling.com" + href
            if not title or not href or href in seen or not title_ok(title):
                continue
            seen.add(href)
            loc = "; ".join(clean(L.get("name") or "") for L in (j.get("locations") or []) if L.get("name"))
            out.append(make_job(config, "college_rippling", title, href, location=loc,
                                description=_detail(href),
                                department=clean(((j.get("department") or {}).get("name")) or "")))
        return out
    except Exception:
        return []
