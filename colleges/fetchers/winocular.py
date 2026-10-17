"""WinOcular job postings (school districts): https://apps2.winocular.com/<district>/jobs/
which redirects to /<District>/workspace/wSpace.exe?Action=wsJobsMain.

config: {"url": "https://apps2.winocular.com/<district>/jobs/", "district": "<district>"}
The main screen lists every posting server-side: each row has an
a[href="javascript:ViewJobPosting('<number>', '<title>')"] link and cells for
type, category, title, posting number, posting date, close date and location;
the body is wSpace.exe?Action=wsJobsDetail&JobPosting=<number> (a dhtmlx
window in the browser, a plain page for us).
Verified against Escambia County School District (9 postings).
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "winocular"
needs_browser = False
URL_RE = re.compile(r"https?://([a-z0-9.-]*winocular\.com)/([A-Za-z0-9_-]+)/", re.I)
VIEW_RE = re.compile(r"ViewJobPosting\('([^']+)'", re.I)
DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
NAV_RE = re.compile(r"^.*?Login to Apply\s*", re.S)


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search(html or "")
    if not m or m.group(2).lower() in ("images", "javascript", "css"):
        return None
    host, district = m.group(1).lower(), m.group(2)
    return {"url": f"https://{host}/{district.lower()}/jobs/", "district": district}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return ""
        return NAV_RE.sub("", strip_html(r.text), count=1)[:20000]
    except Exception:
        return ""


def fetch(config):
    try:
        r = requests.get(config["url"], headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        base = r.url
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for a in soup.find_all("a", href=VIEW_RE):
            num = VIEW_RE.search(a["href"]).group(1)
            title = clean(a.get_text(" ", strip=True))
            if not title or num in seen or not title_ok(title):
                continue
            seen.add(num)
            cells = [clean(c.get_text(" ", strip=True)) for c in a.find_parent("tr").find_all("td")] if a.find_parent("tr") else []
            dates = [c for c in cells if DATE_RE.search(c)]
            m = DATE_RE.search(dates[0]) if dates else None
            posted = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""
            loc = cells[-1] if cells and not DATE_RE.search(cells[-1]) else ""
            href = urljoin(base, f"wSpace.exe?Action=wsJobsDetail&JobPosting={num}")
            out.append(make_job(config, "college_winocular", title, href, location=loc,
                                description=_detail(href), posted=posted,
                                department=cells[2] if len(cells) > 3 else ""))
        return out
    except Exception:
        return []
