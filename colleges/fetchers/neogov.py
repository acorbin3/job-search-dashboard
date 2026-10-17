"""NeoGov boards: https://www.governmentjobs.com/careers/<agency> (also schooljobs.com).

The careers page is an Angular shell, but the server still answers the legacy
list endpoint used by that shell when (and only when) the request carries
X-Requested-With: XMLHttpRequest:

    GET /careers/home/index?agency=<agency>&sort=PostingDate&isDescendingSort=true&page=N

It returns an HTML fragment: "<N> Job Postings found", one li.list-item per
posting (a.item-details-link -> title/href, ul.list-meta li[0] -> location) and
ul.pagination with ?page=N links. Detail pages (/careers/<agency>/jobs/<id>/<slug>)
carry labelled .term-block pairs (Location, Opening Date, Department, ...) and
the description under #details-info.
"""
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "neogov"
needs_browser = False
MAX_POSTINGS = 1000
_DOMAINS = ("governmentjobs.com", "schooljobs.com")
_CAREERS_RE = re.compile(
    r"https?://(?:www\.)?(governmentjobs|schooljobs)\.com/careers/([A-Za-z0-9_-]+)", re.I)
_SKIP_AGENCIES = {"home", "jobs", "search", "login", "index", "agencycustomstylescheme"}
_LIST_HEADERS = {**HEADERS, "X-Requested-With": "XMLHttpRequest",
                 "Accept": "text/html, */*; q=0.01"}


def _match(s):
    for m in _CAREERS_RE.finditer(s or ""):
        agency = m.group(2)
        if agency.lower() not in _SKIP_AGENCIES:
            return m.group(1).lower(), agency
    return None


def detect(url, html=""):
    hit = _match(url) or _match(html)
    if not hit:
        return None
    domain, agency = hit
    return {"url": f"https://www.{domain}.com/careers/{agency}", "agency": agency}


def _base(config):
    """(scheme://host, agency) from config; agency falls back to the URL path."""
    url = (config.get("url") or "").strip()
    p = urlsplit(url)
    host = p.netloc or "www.governmentjobs.com"
    agency = config.get("agency") or ""
    if not agency:
        hit = _match(url)
        agency = hit[1] if hit else ""
    return f"{p.scheme or 'https'}://{host}", agency


def _parse_date(s):
    """'09/11/26' or '09/11/2026 8:00 AM' -> '2026-09-11' ('' if unparseable)."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s or "")
    if not m:
        return ""
    mo, d, y = (int(x) for x in m.groups())
    y = y + 2000 if y < 100 else y
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _list_page(session, host, agency, page):
    """Return (items, total, has_next) for one list page; items = (title, url, location)."""
    r = session.get(f"{host}/careers/home/index",
                    params={"agency": agency, "sort": "PostingDate",
                            "isDescendingSort": "true", "page": page},
                    headers=_LIST_HEADERS, timeout=25)
    if r.status_code != 200:
        return [], 0, False
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select("li.list-item"):
        a = li.select_one("a.item-details-link") or li.find("a", href=re.compile(r"/jobs/\d+"))
        if not a or not a.get("href"):
            continue
        title = clean(a.get_text(" ", strip=True))
        url = urljoin(host, a["href"])
        meta = li.select("ul.list-meta > li")
        loc = clean(meta[0].get_text(" ", strip=True)) if meta else ""
        items.append((title, url, loc))
    total = 0
    n = soup.find(id="job-postings-number")
    if n:
        m = re.search(r"\d+", n.get_text())
        total = int(m.group()) if m else 0
    has_next = bool(soup.find("a", href=re.compile(rf"[?&]page={page + 1}\b")))
    return items, total, has_next


def _detail(session, url):
    """Return (description, location, posted_iso, department) from a job page."""
    try:
        r = session.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return "", "", "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        fields = {}
        for blk in soup.select(".term-block"):
            lbl = blk.select_one(".term-description")
            val = blk.select_one(".span8")
            if lbl and val:
                key = clean(lbl.find(string=True, recursive=False) or lbl.get_text(" ", strip=True))
                fields[key.lower().rstrip(":")] = clean(val.get_text(" ", strip=True))
        loc = fields.get("location", "")
        if not loc:
            p = soup.select_one("p.job-location")
            loc = clean(p.get_text(" ", strip=True)) if p else ""
        posted = _parse_date(fields.get("opening date") or fields.get("posted") or "")
        dept = fields.get("department", "")
        parts = ["\n".join(f"{k.title()}: {v}" for k, v in fields.items() if v)]
        info = soup.find(id="details-info") or soup.select_one(".job-details-main, .job-flyout-details")
        if info:
            parts.append(strip_html(str(info)))
        return "\n\n".join(p for p in parts if p), loc, posted, dept
    except Exception:
        return "", "", "", ""


def fetch(config):
    try:
        host, agency = _base(config)
        if not agency:
            return []
        session = requests.Session()
        out, seen, kept = [], set(), []
        page, scanned = 1, 0
        while page <= 100:
            try:
                items, total, has_next = _list_page(session, host, agency, page)
            except Exception:
                break
            if not items:
                break
            for title, url, loc in items:
                scanned += 1
                if url in seen:
                    continue
                seen.add(url)
                if title_ok(title):
                    kept.append((title, url, loc))
            if not has_next or scanned >= MAX_POSTINGS or (total and scanned >= total):
                break
            page += 1
        for title, url, loc in kept:
            desc, dloc, posted, dept = _detail(session, url)
            out.append(make_job(config, "college_neogov", title, url, dloc or loc,
                                desc, posted, dept))
        return out
    except Exception:
        return []
