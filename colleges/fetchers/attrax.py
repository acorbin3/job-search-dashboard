"""Attrax career sites (server-rendered /jobs?page=N listing on a custom host).

config: {"url": "https://<host>/jobs?options=<n>", "attrax_base": "https://<host>", "attrax_query": "options=<n>"}
Attrax has no vendor host, so detection relies on page markers (attraxbundle.js /
AttraxApplicationBundle / div.attrax-vacancy-tile). Each listing page holds 12
div.attrax-vacancy-tile cards (a.attrax-vacancy-tile__title + location-freetext);
the job page carries the body in div.description-widget and datePosted in JSON-LD.
Verified against careers.covista.com (Adtalem: Chamberlain, Walden).
"""
import re
from urllib.parse import urljoin, urlsplit, parse_qsl

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "attrax"
needs_browser = False

MARKER_RE = re.compile(r"attraxbundle\.js|AttraxApplicationBundle|attrax-vacancy-tile|attraxAntiforgeryToken", re.I)
MAX_PAGES = 60


def detect(url, html=""):
    if not MARKER_RE.search(html or ""):
        return None
    u = urlsplit(url or "")
    if not u.netloc:
        return None
    base = f"{u.scheme or 'https'}://{u.netloc}"
    opts = [f"{k}={v}" for k, v in parse_qsl(u.query) if k == "options"]
    q = "&".join(opts)
    return {"url": f"{base}/jobs" + (f"?{q}" if q else ""), "attrax_base": base, "attrax_query": q}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        box = soup.find("div", class_="description-widget") or soup.find(class_="page-job-details__main-content")
        m = re.search(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})', r.text)
        return (strip_html(str(box))[:20000] if box else ""), (m.group(1) if m else "")
    except Exception:
        return "", ""


def fetch(config):
    try:
        base = config.get("attrax_base") or "{0.scheme}://{0.netloc}".format(urlsplit(config["url"]))
        q = config.get("attrax_query", "")
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            url = f"{base}/jobs?{q + '&' if q else ''}page={page}"
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                break
            tiles = BeautifulSoup(r.text, "html.parser").find_all("div", class_="attrax-vacancy-tile")
            new = 0
            for t in tiles:
                a = t.find("a", class_="attrax-vacancy-tile__title", href=True)
                if not a:
                    continue
                href = urljoin(base, a["href"])
                if href in seen:
                    continue
                seen.add(href)
                new += 1
                title = clean(a.get_text(" ", strip=True))
                if not title or not title_ok(title):
                    continue
                loc_el = t.find(class_="attrax-vacancy-tile__location-freetext")
                loc = clean(loc_el.find(class_="attrax-vacancy-tile__item-value").get_text(" ", strip=True)) if loc_el else ""
                dep_el = t.find(class_="attrax-vacancy-tile__option-department")
                dep = clean(dep_el.find(class_="attrax-vacancy-tile__item-value").get_text(" ", strip=True)) if dep_el else ""
                desc, posted = _detail(href)
                out.append(make_job(config, "college_attrax", title, href, location=loc,
                                    description=desc, posted=posted, department=dep))
            if not new:
                break
        return out
    except Exception:
        return []
