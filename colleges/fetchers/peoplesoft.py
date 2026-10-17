"""Oracle PeopleSoft HCM "Candidate Gateway" (Fluid) career sites.

URL shape: https://<host>/psc/<site>/<portal>/<node>/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL
Examples: University System of Georgia (one host, per-school ?SiteId=NNNNN),
Florida State (jobs.omni.fsu.edu), University of California (careerspub.
universityofcalifornia.edu), FSCJ (jobs.fscj.edu).

The classic HRS_HRAM.HRS_APP_SCHJOB.GBL page answers "not authorized", but the
fluid search-results page renders server-side with a plain GET:

    GET <component>?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant[&SiteId=N]

(the first request bounces to ?cmd=login&errorPg=ckreq, a cookie check; the
same GET with the session cookie succeeds). The HTML carries the form's hidden
IC* fields and the first 50 rows (li#HRS_AGNT_RSLT_I$0_row_N with spans
SCH_JOB_TITLE$N, HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$N, LOCATION$N,
HRS_APP_JBSCH_I_HRS_DEPT_DESCR$N, SCH_OPENED$N) plus "<b>T</b> jobs found".
More rows come from the Fluid ICAJAX POST (same URL, hidden fields +
ICAction=HRS_AGNT_RSLT_I$hdown$0&ICAJAX=1), whose XML response contains the
cumulative grid (100, 150, ... rows; the server caps display at 999). A keyword
search is ICAction=HRS_SCH_WRK_FLU_HRS_SEARCH_BTN with
HRS_SCH_WRK_HRS_SCH_TEXT100=<kw>; we use it when the board is large.
Posting pages are public GETs:
    <component>?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId=N&JobOpeningId=ID&PostingSeq=1
(SiteId is required; 1 is the default site when the board has none) with the
description in span#HRS_SCH_PSTDSC_DESCRLONG$N blocks.
"""
import re
from datetime import datetime
from urllib.parse import urlsplit, parse_qs

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "peoplesoft"
needs_browser = False
MAX_POSTINGS = 1000
KEYWORD_THRESHOLD = 300          # above this many postings, keyword-search instead of paging all
QUERIES = ["adjunct", "instructor", "lecturer", "faculty", "professor"]
_PAGE_TIMEOUT = 60

_COMP_RE = re.compile(
    r"https?://[^/\s\"'<>]+/ps[cp]/[^/\s\"'<>]+/[^/\s\"'<>]+/[^/\s\"'<>]+/c/"
    r"(?:HRS_HRAM_FL\.HRS_CG_SEARCH_FL|HRS_HRAM\.HRS_APP_SCHJOB|HRS_HRAM_FL\.[A-Z_]+)\.GBL", re.I)
_MARKERS = ("HRS_CG_SEARCH_FL", "HRS_HRAM_FL", "HRS_APP_SCHJOB", "HRS_APP_JBPST")
_AJAX_HEADERS = {**HEADERS, "X-Requested-With": "XMLHttpRequest",
                 "Accept": "*/*", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}


def _component(s):
    """Return the canonical /psc/ component URL (…HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL) found in s."""
    m = _COMP_RE.search(s or "")
    if not m:
        return ""
    comp = re.sub(r"/psp/", "/psc/", m.group(0), count=1, flags=re.I)
    return re.sub(r"/c/[A-Z_]+\.[A-Z_]+\.GBL$", "/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL", comp, flags=re.I)


def detect(url, html=""):
    url = url or ""
    html = html or ""
    comp = _component(url) or _component(html)
    if not comp:
        p = urlsplit(url)
        if re.search(r"/ps[cp]/", p.path) and any(k in url or k in html for k in _MARKERS):
            # HRMS-ish URL whose component name is unusual: keep the given path.
            comp = f"{p.scheme}://{p.netloc}{p.path}"
        else:
            return None
    site_id = ""
    for src in (url, html):
        m = re.search(r"SiteId=(\d+)", src)
        if m:
            site_id = m.group(1)
            break
    q = "?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant"
    if site_id:
        q += f"&SiteId={site_id}"
    return {"url": comp + q, "site_id": site_id}


def _base(config):
    """(component_url, site_id) from config."""
    url = (config.get("url") or "").strip()
    comp = _component(url) or url.split("?")[0]
    site_id = config.get("site_id") or ""
    if not site_id:
        site_id = (parse_qs(urlsplit(url).query).get("SiteId") or [""])[0]
    return comp, site_id


def _parse_date(s):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    if not m:
        return ""
    try:
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _hidden(html):
    return dict(re.findall(r"<input type='hidden' name='(\w+)'[^>]*value='([^']*)'", html))


def _get(session, url):
    r = session.get(url, headers=HEADERS, timeout=_PAGE_TIMEOUT)
    if "errorPg=ckreq" in r.url or "cmd=login" in r.url:   # cookie check: retry with session cookie
        r = session.get(url, headers=HEADERS, timeout=_PAGE_TIMEOUT)
    return r


def _ajax(session, comp, referer, hidden, action, extra=None):
    data = {**hidden, "ICAction": action, "ICAJAX": "1", "ICNAVTYPEDROPDOWN": "0", **(extra or {})}
    r = session.post(comp, data=data, headers={**_AJAX_HEADERS, "Referer": referer}, timeout=_PAGE_TIMEOUT * 2)
    text = r.text if r.status_code == 200 else ""
    # The response may re-emit the hidden fields; otherwise the state number just advances.
    new = _hidden(text) if "PSHIDDENFIELDS" in text else {}
    hidden = {**hidden, **{k: v for k, v in new.items() if k.startswith("IC")}}
    if not new:
        try:
            hidden["ICStateNum"] = str(int(hidden.get("ICStateNum", "1")) + 1)
        except ValueError:
            pass
    return text, hidden


def _rows(html):
    """Parse result rows -> list of dicts keyed title/id/location/department/posted."""
    out = []
    for m in re.finditer(r"id='SCH_JOB_TITLE\$(\d+)'\s*>(.*?)</span>", html, re.S):
        n = m.group(1)

        def field(fid):
            mm = re.search(rf"id='{re.escape(fid)}\${n}'\s*>(.*?)</span>", html, re.S)
            return clean(strip_html(mm.group(1))) if mm else ""

        jid = field("HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID") or field("HRS_JOB_OPENING_ID")
        if not jid:
            continue
        out.append({"title": clean(strip_html(m.group(2))), "id": re.sub(r"\D", "", jid),
                    "location": field("LOCATION").replace("\xa0", "").strip(),
                    "department": field("HRS_APP_JBSCH_I_HRS_DEPT_DESCR").replace("\xa0", "").strip(),
                    "posted": _parse_date(field("SCH_OPENED"))})
    return out


def _total(html):
    m = re.search(r"<b>(\d+)</b>\s*jobs found", html)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+) rows", html)
    return int(m.group(1)) if m else 0


def _page_all(session, comp, list_url, html, hidden, cap):
    """Walk the result grid with 'more' until cap/total; returns rows (cumulative)."""
    rows = _rows(html)
    total = _total(html)
    limit = min(cap, total or cap, 999)
    guard = 0
    while len(rows) < limit and "HRS_AGNT_RSLT_I$hdown$0" in html and guard < 25:
        guard += 1
        html, hidden = _ajax(session, comp, list_url, hidden, "HRS_AGNT_RSLT_I$hdown$0")
        new = _rows(html)
        if len(new) <= len(rows):
            break
        rows = new
    return rows


def _keyword_rows(session, comp, list_url, cap):
    """Run each keyword search from a fresh list state; returns merged rows."""
    merged, seen = [], set()
    for kw in QUERIES:
        try:
            r = _get(session, list_url)
            if r.status_code != 200:
                continue
            hidden = _hidden(r.text)
            if "ICSID" not in hidden:
                continue
            html, hidden = _ajax(session, comp, list_url, hidden, "HRS_SCH_WRK_FLU_HRS_SEARCH_BTN",
                                 {"HRS_SCH_WRK_HRS_SCH_TEXT100": kw})
            for row in _page_all(session, comp, list_url, html, hidden, cap):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    merged.append(row)
        except Exception:
            continue
        if len(merged) >= cap:
            break
    return merged


def _posting_url(comp, site_id, jid):
    return (f"{comp}?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId={site_id or '1'}"
            f"&JobOpeningId={jid}&PostingSeq=1")


def _detail(session, url):
    """Return (description, location, department) from a posting page."""
    try:
        r = _get(session, url)
        if r.status_code != 200 or "HRS_APP_JBPST_FL" not in r.text:
            return "", "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        parts, loc, dept = [], "", ""
        for lbl in soup.select("span.ps-label"):
            key = clean(lbl.get_text(" ", strip=True)).lower().rstrip(":")
            val = lbl.find_parent("div", class_="ps_box-label")
            val = val.find_next_sibling("span", class_="ps_box-value") if val else None
            if not val:
                continue
            v = clean(val.get_text(" ", strip=True)).replace("\xa0", "").strip()
            if key == "location" and v and not loc:
                loc = v
            elif key == "department" and v and not dept:
                dept = v
            if key in ("job id", "location", "full/part time", "regular/temporary", "department",
                       "job title") and v:
                parts.append(f"{key.title()}: {v}")
        groups = soup.select("div[id^='win0divHRS_SCH_WRK_DESCR100$']")
        for grp in groups:
            h = grp.select_one("h2, .ps_header-group")
            body = grp.select_one("span[id^='HRS_SCH_PSTDSC_DESCRLONG']")
            if body:
                head = clean(h.get_text(" ", strip=True)) if h else ""
                parts.append((head + "\n" if head else "") + strip_html(str(body)))
        if not groups:  # fallback: any description blocks
            for body in soup.select("span[id^='HRS_SCH_PSTDSC_DESCRLONG'], "
                                    "div[id^='win0divHRS_SCH_WRK_HRS_DESCRLONG']"):
                parts.append(strip_html(str(body)))
        return "\n\n".join(p for p in parts if p), loc, dept
    except Exception:
        return "", "", ""


def fetch(config):
    try:
        comp, site_id = _base(config)
        if not comp:
            return []
        list_url = f"{comp}?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant"
        if site_id:
            list_url += f"&SiteId={site_id}"
        session = requests.Session()
        r = _get(session, list_url)
        if r.status_code != 200 or "HRS_AGNT_RSLT_I" not in r.text:
            return []
        hidden = _hidden(r.text)
        total = _total(r.text)
        if total > KEYWORD_THRESHOLD or (total == 0 and len(_rows(r.text)) >= 50):
            rows = _keyword_rows(session, comp, list_url, MAX_POSTINGS)
        else:
            rows = _page_all(session, comp, list_url, r.text, hidden, MAX_POSTINGS)
        out, seen = [], set()
        for row in rows[:MAX_POSTINGS]:
            if row["id"] in seen or not title_ok(row["title"]):
                continue
            seen.add(row["id"])
            url = _posting_url(comp, site_id, row["id"])
            desc, dloc, ddept = _detail(session, url)
            out.append(make_job(config, "college_peoplesoft", row["title"], url,
                                row["location"] or dloc, desc, row["posted"],
                                row["department"] or ddept))
        return out
    except Exception:
        return []
