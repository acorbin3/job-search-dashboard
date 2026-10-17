"""SAP SuccessFactors career sites.

Two flavours exist:
  * Career Site Builder (CSB) vanity hosts such as https://jobs.uc.edu – the
    modern ones render search results client-side from a JSON endpoint:
      POST https://<host>/services/recruiting/v1/jobs
           {"locale":"en_US","pageNumber":0,"keywords":"adjunct", ...}
    returning {"jobSearchResult":[{"response":{...}}],"totalJobs":N} (10/page).
    Older CSB hosts render the same list server-side at /search/?q=<kw>&startrow=N
    (tr.data-row / a.jobTitle-link). Detail pages live at
    /job/<urlTitle>/<id>-<locale> and carry the body in [itemprop=description].
  * Legacy https://career<N>.successfactors.com/career?company=<co>&career_ns=
    job_listing_summary pages; those are a JSF app rendered client-side and
    usually redirect to a CSB host, which we follow.
Config: {"url", "company"} (company is informational; the CSB host is the key).
A host whose certificate fails (apply.dadeschools.net, expired) is retried
without TLS verification.
Verified against University of Cincinnati (jobs.uc.edu, company=UCPROD) and
Miami-Dade County Public Schools (apply.dadeschools.net, 303 jobs).
"""
import html as _html
import re
from urllib.parse import urljoin, urlsplit, parse_qs
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "successfactors"


def _request(method, url, **kw):
    """requests.get/post that retries once without TLS verification on a certificate error."""
    try:
        return getattr(requests, method)(url, **kw)
    except requests.exceptions.SSLError:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return getattr(requests, method)(url, verify=False, **kw)

needs_browser = False
QUERIES = ["adjunct", "instructor", "lecturer", "faculty", "professor"]
MAX_JOBS = 1000
JSON_HEADERS = dict(HEADERS, Accept="application/json")
_MARKERS = ("successfactors.com", "rmkcdn.successfactors.com", "jobs2web", "/platform/js/j2w/",
            "sitebuilderframework", "career_ns=")


def detect(url, html=""):
    h = html or ""
    p = urlsplit(url)
    company = parse_qs(p.query).get("company", [""])[0]
    if not company:
        m = re.search(r"[?&;](?:bplte_)?company=([A-Za-z0-9_-]+)", h)
        company = m.group(1) if m else ""
    if "successfactors" in p.netloc.lower() or any(k in h for k in _MARKERS):
        return {"url": url, "company": company}
    return None


def _host(config):
    p = urlsplit(config["url"])
    return f"{p.scheme}://{p.netloc}"


def _parse_us_date(s):
    s = clean(s)
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _detail(url):
    try:
        r = _request("get", url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return "", "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        body = (soup.select_one("[itemprop=description]") or soup.select_one(".jobdescription")
                or soup.select_one(".job") or soup.find("main"))
        desc = strip_html(str(body)) if body else ""
        loc = soup.select_one("[itemprop=jobLocation], .jobGeoLocation, .joblocation")
        loc = clean(loc.get_text(" ", strip=True)) if loc else ""
        posted = ""
        dp = soup.select_one("[itemprop=datePosted]")
        if dp:
            posted = _parse_us_date(dp.get("content") or dp.get_text(strip=True))
        if not posted:
            m = re.search(r"(?:Date|Posted)\s*:?\s*</?[^>]*>?\s*(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]{2} \d{1,2}, \d{4})",
                          r.text)
            posted = _parse_us_date(m.group(1)) if m else ""
        return desc, loc, posted
    except Exception:
        return "", "", ""


def _api_search(host, keyword):
    """Modern CSB JSON endpoint. Returns list of (title, url, loc, posted) or None if unsupported."""
    out, page = [], 0
    while len(out) < MAX_JOBS:
        body = {"locale": "en_US", "pageNumber": page, "sortBy": "", "keywords": keyword,
                "location": "", "facetFilters": {}, "brand": "", "skills": [], "categoryId": 0,
                "alertId": "", "rcmCandidateId": ""}
        try:
            r = _request("post", f"{host}/services/recruiting/v1/jobs", json=body,
                              headers=JSON_HEADERS, timeout=30)
            if r.status_code != 200:
                return None if page == 0 else out
            d = r.json()
        except Exception:
            return None if page == 0 else out
        rows = d.get("jobSearchResult") or []
        for row in rows:
            j = row.get("response") or row
            jid = str(j.get("id") or "")
            title = clean(j.get("unifiedStandardTitle") or j.get("title") or "")
            if not jid or not title:
                continue
            slug = _html.unescape(j.get("unifiedUrlTitle") or j.get("urlTitle") or "job")
            locale = (j.get("supportedLocales") or ["en_US"])[0]
            url = f"{host}/job/{slug}/{jid}-{locale}"
            loc = clean(strip_html(" ".join(j.get("jobLocationShort") or [])))
            posted = _parse_us_date(j.get("unifiedStandardStart") or "")
            out.append((title, url, loc, posted))
        total = int(d.get("totalJobs") or 0)
        page += 1
        if not rows or page * max(len(rows), 1) >= total:
            break
    return out


def _html_search(host, keyword):
    """Older CSB: server-rendered /search/?q=&startrow=."""
    out, start = [], 0
    while len(out) < MAX_JOBS:
        try:
            r = _request("get", f"{host}/search/", params={"q": keyword, "startrow": start},
                             headers=HEADERS, timeout=30)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            break
        rows = soup.select("tr.data-row")
        for tr in rows:
            a = tr.select_one("a.jobTitle-link") or tr.find("a", href=re.compile(r"/job/"))
            if not a:
                continue
            loc = tr.select_one(".jobLocation, .colLocation")
            date = tr.select_one(".jobDate, .colDate")
            out.append((clean(a.get_text(" ", strip=True)), urljoin(host, a["href"]),
                        clean(loc.get_text(" ", strip=True)) if loc else "",
                        _parse_us_date(date.get_text(strip=True)) if date else ""))
        if not rows:
            break
        start += len(rows)
        nxt = soup.select_one("a.paginationItemLast, .pagination-next, a[rel=next]")
        if not nxt or len(rows) < 10:
            break
    return out


def fetch(config):
    try:
        host = _host(config)
        out, seen = [], set()
        for q in QUERIES:
            rows = _api_search(host, q)
            if rows is None:
                rows = _html_search(host, q)
            for title, url, loc, posted in rows:
                if url in seen or not title_ok(title):
                    continue
                seen.add(url)
                desc, dloc, dposted = _detail(url)
                out.append(make_job(config, "college_successfactors", title, url,
                                    loc or dloc, desc, posted or dposted))
                if len(out) >= MAX_JOBS:
                    return out
        return out
    except Exception:
        return []
