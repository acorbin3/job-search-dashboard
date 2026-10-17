"""Avature career sites: https://<tenant>.avature.net/careers/SearchJobs

config: {"url": "https://<tenant>.avature.net/careers/SearchJobs", "tenant": "<tenant>"}
Server-rendered rows (li.listSingleColumnItem / li.listDetailItem with a
/careers/JobDetail/<slug>/<id> link and MiscDataItem spans for location and
ref #), paginated with ?jobOffset=N (10 per page). The detail page carries
div.detailDescription and a JSON-LD datePosted.
Verified against University of Phoenix (uop).
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "avature"
needs_browser = False
TENANT_RE = re.compile(r"https?://([a-z0-9-]+)\.avature\.net(/[^\s\"'?#]*?/)?(?:SearchJobs|JobDetail)", re.I)
PAGE = 10
MAX_PAGES = 60


def detect(url, html=""):
    m = TENANT_RE.search(url or "") or TENANT_RE.search(html or "")
    if not m or m.group(1).lower() in ("www", "app", "api"):
        return None
    tenant = m.group(1).lower()
    path = m.group(2) or "/careers/"
    return {"url": f"https://{tenant}.avature.net{path}SearchJobs", "tenant": tenant}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        box = soup.find("div", class_=re.compile(r"detailDescription|jobDescription"))
        m = re.search(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})', r.text)
        return (strip_html(str(box))[:20000] if box else ""), (m.group(1) if m else "")
    except Exception:
        return "", ""


def fetch(config):
    try:
        base = config["url"]
        out, seen = [], set()
        for page in range(MAX_PAGES):
            r = requests.get(base, params={"jobOffset": page * PAGE}, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            new = 0
            for li in soup.find_all("li", class_=re.compile(r"listSingleColumnItem|listDetailItem")):
                a = li.find("a", href=re.compile(r"/JobDetail/", re.I))
                if not a or li.find_parent("section", class_=re.compile("Featured")):   # featured rows lack the location
                    continue
                href = urljoin(base, a["href"]).split("?")[0]
                title = clean(a.get_text(" "))
                if href in seen:
                    continue
                seen.add(href)
                new += 1
                if not title_ok(title):
                    continue
                misc = [clean(s.get_text(" ")).rstrip(".") for s in li.find_all("span", class_=re.compile("MiscDataItem"))]
                loc = next((m for m in misc if m and not re.match(r"(Ref\s*#|Virtual Eligible|Posted)", m, re.I)), "")
                if any(re.match(r"Virtual Eligible:\s*Yes", m, re.I) for m in misc) and "virtual" not in loc.lower():
                    loc = f"{loc} (Virtual eligible)".strip()
                desc, posted = _detail(href)
                out.append(make_job(config, "college_avature", title, href, location=loc,
                                    description=desc, posted=posted))
            if new < PAGE:
                break
        return out
    except Exception:
        return []
