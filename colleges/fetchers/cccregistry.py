"""California Community Colleges Registry aggregator: https://www.cccregistry.org

ASP.NET WebForms site, but the search forms (/jobs/searchForm.aspx and
/jobs/searchFormNew.aspx) carry no __VIEWSTATE and a POST of their fields just
echoes the form back, so keyword search is not usable. What does work are the
plain-GET per-district listings:

    GET /jobs/chooseDistrict.aspx?display=cc            -> links to every district
    GET /jobs/districtSearchResults.aspx?districtID=N   -> table: title/posted/
                                                           closing/type/salary
    GET /jobs/viewPosting.aspx?postingID=N              -> labelled detail rows

We walk every district (~78 pages), keep Faculty rows whose title is about
computing (or a generic adjunct/part-time pool), drop rows whose closing date
has passed, and fetch the detail page only for survivors (dropping any whose
detail page redirects home - the listings still show some deleted IDs). One
request per second; a full run takes several minutes.

Caveat: as of Oct 2026 the site is frozen - newest sitemap lastmod and every
posting date is 2024 - so only still-open "Continuous"/"Until Filled" pools
come through, carrying their original 2023-24 posted dates.
"""
import re
import time
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, clean

name = "cccregistry"
needs_browser = False
DELAY = 1.0
MAX_DETAILS = 150
DISCIPLINE_RE = re.compile(
    r"comput|\bcis\b|\bcs\b|information (technology|systems)|\bit\b|cyber|network|"
    r"software|programm|data science|informatics|web dev|\bai\b|artificial intelligence|"
    r"machine learning|all disciplines|"
    # generic (discipline-less) pools, e.g. "Adjunct Faculty Pool", "Part-Time Instructor Pool"
    r"^\W*(adjunct|part[- ]time|hourly)\s+(faculty|instructor)s?\s+pool\b\W*(\([^)]*\))?\W*$", re.I)
OPEN_ENDED_RE = re.compile(r"continuous|until filled|open|ongoing|pool", re.I)
POSTING_RE = re.compile(r"viewPosting\.aspx\?postingID=(\d+)", re.I)


def detect(url, html=""):
    return None


def _get(url, **kw):
    """Polite GET with one retry; the host occasionally drops connections."""
    for attempt in (1, 2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, **kw)
        except requests.RequestException:
            r = None
        time.sleep(DELAY * attempt)
        if r is not None and r.status_code == 200:
            return r
    return None


def _parse_date(s):
    s = clean(s).replace(",", "")
    for fmt in ("%B %d %Y", "%A %B %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _still_open(closing):
    if not closing or OPEN_ENDED_RE.search(closing):
        return True
    d = _parse_date(closing)
    return d is None or d >= date.today()


def _districts(base):
    r = _get(f"{base}/jobs/chooseDistrict.aspx?display=cc")
    if r is None:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"districtSearchResults\.aspx\?districtID=\d+", re.I)):
        href = urljoin(f"{base}/jobs/", a["href"])
        dname = re.sub(r"\s*\(\d+\)\s*$", "", clean(a.get_text(" ", strip=True)))
        if href not in seen:
            seen.add(href)
            out.append((dname, href))
    return out


def _rows(html, base):
    """Yield (title, url, posted, closing, job_type) for a district results table."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=POSTING_RE):
        title = clean(a.get_text(" ", strip=True))
        tr = a.find_parent("tr")
        if not title or tr is None:
            continue
        cells = tr.find_all("td")
        first = cells[0].get_text(" ", strip=True) if cells else ""
        m = re.search(r"Posted On:\s*([A-Za-z]+ \d{1,2}, \d{4})", first)
        posted = m.group(1) if m else ""
        m = re.search(r"Closing Date:\s*(.+?)(?:\s*$)", first)
        closing = m.group(1) if m else (clean(cells[2].get_text()) if len(cells) > 2 else "")
        job_type = clean(cells[1].get_text()) if len(cells) > 1 else ""
        yield title, urljoin(f"{base}/jobs/", a["href"]), posted, closing, job_type


def _detail(url):
    """Return (description_text, fields dict) from a viewPosting.aspx page.

    Returns None when the posting is gone: the district listing still shows
    some IDs whose viewPosting.aspx 302s back to the home page.
    """
    r = _get(url)
    if r is None:
        return "", {}
    if "viewposting" not in r.url.lower() or 'id="pnlPosting"' not in r.text:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    fields, lines = {}, []
    for cell in soup.find_all(["td", "th", "dt", "label", "b", "strong"]):
        label = clean(cell.get_text(" ", strip=True))
        if not label.endswith(":") or len(label) > 40:
            continue
        nxt = cell.find_next_sibling(["td", "dd"]) or cell.find_next(["td", "dd", "span", "div"])
        val = clean(nxt.get_text(" ", strip=True)) if nxt else ""
        if val and val != label:
            fields[label.rstrip(":").lower()] = val
            lines.append(f"{label} {val}")
    links = [a["href"] for a in soup.find_all("a", href=re.compile(r"^https?://"))
             if "cccregistry.org" not in a["href"]]
    if links:
        lines.append("Links: " + " ".join(dict.fromkeys(links)))
    return "\n".join(lines), fields


def fetch(config):
    base = (config.get("url") or "https://www.cccregistry.org").rstrip("/")
    out, seen, details = [], set(), 0
    try:
        districts = _districts(base)
    except Exception:
        return out
    for dname, durl in districts:
        try:
            r = _get(durl)
            if r is None:
                continue
            rows = list(_rows(r.text, base))
        except Exception:
            continue
        for title, url, posted, closing, job_type in rows:
            if url in seen or (job_type and job_type.lower() != "faculty"):
                continue
            if not DISCIPLINE_RE.search(title) or not title_ok(title) or not _still_open(closing):
                continue
            seen.add(url)
            desc, fields = "", {}
            if details < MAX_DETAILS:
                details += 1
                try:
                    got = _detail(url)
                except Exception:
                    got = ("", {})
                if got is None:          # dead posting: listing is stale
                    continue
                desc, fields = got
            college = fields.get("district/college") or dname
            pd = _parse_date(fields.get("date posted", "")) or _parse_date(posted)
            posted_iso = pd.isoformat() if pd else ""
            header = f"{title}\nDistrict/College: {college}\nClosing Date: {closing}\n"
            job = make_job(config, "college_cccregistry", title, url, f"{college}, CA",
                           header + desc, posted_iso, fields.get("job classification", ""))
            job.company = college
            out.append(job)
    return out
