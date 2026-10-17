"""PrismHR Hire (ex-HiringThing) career pages: https://<slug>.prismhr-hire.com/
(also <slug>.hiringthing.com and the Rippling-branded <slug>.rippling-ats.com)

config: {"url": "https://<host>/", "host": "<host>"}
The landing page lists every opening server-side (a[href^=/job/<id>/] with
the h2 title, .job-category and a location line); the job page carries the
body in .job-content-body; every host shares the layout (assets.applicant-tracking.com).
Verified against Naropa University (prismhr-hire) and Ada Developers Academy (rippling-ats).
"""
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "prismhr"
needs_browser = False

HOST_RE = re.compile(r"https?://([a-z0-9-]+\.(?:prismhr-hire|hiringthing|rippling-ats)\.com)", re.I)


def detect(url, html=""):
    m = HOST_RE.search(url or "") or HOST_RE.search(html or "")
    if not m or m.group(1).split(".")[0] in ("www", "app"):
        return None
    host = m.group(1).lower()
    return {"url": f"https://{host}/", "host": host}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        box = soup.find(class_="job-content-body") or soup.find(id="careers-page-job")
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        host = config.get("host") or urlsplit(config["url"]).netloc
        url = f"https://{host}/"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"^/job/\d+")):
            h = a.find(["h2", "h3"])
            title = clean((h or a).get_text(" ", strip=True))
            jid = re.search(r"/job/(\d+)", a["href"]).group(1)
            if not title or title.lower() in ("learn more", "apply") or jid in seen or not title_ok(title):
                continue
            seen.add(jid)
            href = urljoin(url, a["href"])
            card = a.find_parent(class_=re.compile("job", re.I)) or a.parent
            cat = card.find(class_="job-category") if card else None
            loc_el = card.find(class_=re.compile("location", re.I)) if card else None
            out.append(make_job(config, "college_prismhr", title, href,
                                location=clean(loc_el.get_text(" ", strip=True)) if loc_el else "",
                                description=_detail(href),
                                department=clean(cat.get_text(" ", strip=True)) if cat else ""))
        return out
    except Exception:
        return []
