"""ApplicantStack boards: https://<slug>.applicantstack.com/x/openings

config: {"url": ..., "astack_slug": "<slug>"}
The openings page is a server-rendered table: title link /x/detail/<id>,
then type, date posted, location and department columns. The detail page
carries the body in .listing_description.
Verified against University of Mary Hardin-Baylor.
"""
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "applicantstack"
needs_browser = False

SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.applicantstack\.com", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() in ("www", "app"):
        return None
    slug = m.group(1).lower()
    return {"url": f"https://{slug}.applicantstack.com/x/openings", "astack_slug": slug}


def _date(s):
    try:
        return datetime.strptime(clean(s), "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        box = BeautifulSoup(r.text, "html.parser").find(class_="listing_description")
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        slug = config.get("astack_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        url = f"https://{slug}.applicantstack.com/x/openings"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"/x/detail/", re.I)):
            title = clean(a.get_text(" ", strip=True))
            href = urljoin(url, a["href"])
            if not title or href in seen or not title_ok(title):
                continue
            seen.add(href)
            tr = a.find_parent("tr")
            cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")] if tr else []
            posted = next((_date(c) for c in cells if _date(c)), "")
            loc = cells[3] if len(cells) > 3 and cells[3] != "N/A" else ""
            dept = cells[4] if len(cells) > 4 else ""
            out.append(make_job(config, "college_applicantstack", title, href, location=loc,
                                description=_detail(href), posted=posted, department=dept))
        return out
    except Exception:
        return []
