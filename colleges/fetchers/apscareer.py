"""APS Payroll career portals: https://<slug>.apscareerportal.com/jobs

config: {"url": ..., "aps_slug": "<slug>"}
/jobs?locale=en-US is server-rendered (one <a href="/jobs/<id>"> per opening
with .job-title .name and .job-location, no paging seen); the detail page
carries the body in .job-description.
Verified against Nelson University.
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "apscareer"
needs_browser = False

SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.apscareerportal\.com", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() == "www":
        return None
    slug = m.group(1).lower()
    return {"url": f"https://{slug}.apscareerportal.com/jobs?locale=en-US", "aps_slug": slug}


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
        slug = config.get("aps_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        url = f"https://{slug}.apscareerportal.com/jobs?locale=en-US"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"^/jobs/\d+")):
            name_el = a.find(class_="name")
            title = clean((name_el or a).get_text(" ", strip=True))
            href = urljoin(url, a["href"])
            if not title or href in seen or not title_ok(title):
                continue
            seen.add(href)
            loc_el = a.find(class_="job-location")
            loc = clean(loc_el.get_text(" ", strip=True)).replace("Location:", "").strip() if loc_el else ""
            # "TX-Waxahachie-75165" -> "Waxahachie, TX"
            m = re.match(r"^([A-Z]{2})-([^-]+)-\d{5}$", loc)
            if m:
                loc = f"{m.group(2)}, {m.group(1)}"
            out.append(make_job(config, "college_apscareer", title, href, location=loc,
                                description=_detail(href)))
        return out
    except Exception:
        return []
