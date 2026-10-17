"""TriNet Hire boards: https://app.trinethire.com/companies/<id>-<slug>/jobs

config: {"url": ..., "company": "<id>-<slug>"}
The /jobs page is a server-rendered table (tr.job: title link, td.type,
td.location); the job page carries the body in div.job-descr.
Verified against San Francisco Bay University.
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "trinethire"
needs_browser = False

COMPANY_RE = re.compile(r"https?://app\.trinethire\.com/companies/([0-9]+-[a-z0-9-]+)", re.I)


def detect(url, html=""):
    m = COMPANY_RE.search(url or "") or COMPANY_RE.search(html or "")
    if not m:
        return None
    return {"url": f"https://app.trinethire.com/companies/{m.group(1)}/jobs", "company": m.group(1)}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        box = BeautifulSoup(r.text, "html.parser").find(class_="job-descr")
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        company = config.get("company") or (COMPANY_RE.search(config["url"]) or [None, ""])[1]
        url = f"https://app.trinethire.com/companies/{company}/jobs"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for tr in soup.select("tr.job"):
            a = tr.find("a", href=re.compile(r"/jobs/\d+"))
            if not a:
                continue
            title = clean(a.get_text(" ", strip=True))
            href = urljoin(url, a["href"])
            if not title or href in seen or not title_ok(title):
                continue
            seen.add(href)
            loc_el, type_el = tr.find("td", class_="location"), tr.find("td", class_="type")
            out.append(make_job(config, "college_trinethire", title, href,
                                location=clean(loc_el.get_text(" ", strip=True)) if loc_el else "",
                                description=_detail(href),
                                department=clean(type_el.get_text(" ", strip=True)) if type_el else ""))
        return out
    except Exception:
        return []
