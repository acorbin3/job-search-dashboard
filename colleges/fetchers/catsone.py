"""CATS career portals: https://<slug>.catsone.com/careers/<portal_id>

config: {"url": ..., "host": "<slug>.catsone.com", "portal": "<portal_id>"}
The portal page is server-rendered: one a.table-row per opening
(/careers/<portal>/jobs/<id>-<slug>) with the title cell and a status cell;
the job page carries the body in .job-description.
Verified against Washington Adventist University.
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "catsone"
needs_browser = False

URL_RE = re.compile(r"https?://([a-z0-9-]+\.catsone\.com)/careers/(\d+)", re.I)


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search(html or "")
    if not m:
        return None
    host, portal = m.group(1).lower(), m.group(2)
    return {"url": f"https://{host}/careers/{portal}", "host": host, "portal": portal}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        box = BeautifulSoup(r.text, "html.parser").find(class_=re.compile(r"^job-description"))
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        m = URL_RE.search(config["url"])
        host = config.get("host") or (m.group(1) if m else "")
        portal = config.get("portal") or (m.group(2) if m else "")
        url = f"https://{host}/careers/{portal}"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for a in soup.find_all("a", href=re.compile(rf"/careers/{portal}/jobs/\d+")):
            t = a.find(class_=re.compile("title"))
            title = clean((t or a).get_text(" ", strip=True))
            href = urljoin(url, a["href"])
            if not title or href in seen or not title_ok(title):
                continue
            seen.add(href)
            cells = [clean(c.get_text(" ", strip=True)) for c in a.find_all(class_="data-cell")]
            out.append(make_job(config, "college_catsone", title, href,
                                description=_detail(href),
                                department=cells[1] if len(cells) > 1 else ""))
        return out
    except Exception:
        return []
