"""JazzHR hosted boards: https://<slug>.applytojob.com/apply

config: {"url": "https://<slug>.applytojob.com/apply", "jazz_slug": "<slug>"}
The /apply page is server-rendered: one li.list-group-item per opening with
the title link, location (fa-map-marker) and department (fa-sitemap); the
detail page carries the body in #job-description.
Verified against Alfred University (also Hartwick, Franklin Pierce, Lycoming).
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "jazzhr"
needs_browser = False

SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.applytojob\.com", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() in ("app", "www"):
        return None
    slug = m.group(1).lower()
    return {"url": f"https://{slug}.applytojob.com/apply", "jazz_slug": slug}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        box = BeautifulSoup(r.text, "html.parser").find(id="job-description")
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        slug = config.get("jazz_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        url = f"https://{slug}.applytojob.com/apply"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for li in soup.select("li.list-group-item"):
            a = li.find("a", href=re.compile(r"/apply/[A-Za-z0-9]+", re.I))
            if not a:
                continue
            title = clean(a.get_text(" ", strip=True))
            href = urljoin(url, a["href"])
            if not title or href in seen or not title_ok(title):
                continue
            seen.add(href)
            loc = dept = ""
            for item in li.select("ul.list-group-item-text li"):
                icon = item.find("i")
                cls = " ".join(icon.get("class", [])) if icon else ""
                txt = clean(item.get_text(" ", strip=True))
                if "map-marker" in cls:
                    loc = txt
                elif "sitemap" in cls:
                    dept = txt
            out.append(make_job(config, "college_jazzhr", title, href, location=loc,
                                description=_detail(href), department=dept))
        return out
    except Exception:
        return []
