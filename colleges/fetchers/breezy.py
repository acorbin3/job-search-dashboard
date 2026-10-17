"""Breezy HR boards: https://<slug>.breezy.hr/

config: {"url": "https://<slug>.breezy.hr/", "breezy_slug": "<slug>"}
GET /json lists every open position (name, url, location.name, department,
published_date); the /p/<id> page carries the body in div.description.
Verified against Warren Wilson College (also Reed College).
"""
import re

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "breezy"
needs_browser = False

SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() in ("app", "www", "api", "attachments-cdn"):
        return None
    slug = m.group(1).lower()
    return {"url": f"https://{slug}.breezy.hr/", "breezy_slug": slug}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        box = BeautifulSoup(r.text, "html.parser").find("div", class_="description")
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        slug = config.get("breezy_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        r = requests.get(f"https://{slug}.breezy.hr/json", headers=dict(HEADERS, Accept="application/json"), timeout=25)
        if r.status_code != 200 or not r.text.lstrip().startswith("["):
            return []
        out, seen = [], set()
        for j in r.json():
            title = clean(j.get("name") or "")
            href = j.get("url") or f"https://{slug}.breezy.hr/p/{j.get('id', '')}"
            if not title or href in seen or not title_ok(title):
                continue
            seen.add(href)
            loc = clean(((j.get("location") or {}).get("name")) or "")
            posted = (j.get("published_date") or "")[:10]
            out.append(make_job(config, "college_breezy", title, href, location=loc,
                                description=_detail(href), posted=posted,
                                department=clean(j.get("department") or "")))
        return out
    except Exception:
        return []
