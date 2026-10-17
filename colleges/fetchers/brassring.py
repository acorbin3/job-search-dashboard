"""IBM Kenexa BrassRing (TGnewUI) boards at colleges and school districts:
  https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=<pid>&siteid=<sid>
  (also the legacy jobs.brassring.com/1033/ASP/TG/cim_home.asp?partnerid=&siteid= links)

config: {"url", "partnerid": "<pid>", "siteid": "<sid>"}
A requests.Session GET of the Home page sets the cookies; then
POST /TGnewUI/Search/Ajax/ProcessSortAndShowMoreJobs {partnerId, siteId,
pageNumber, SortType "LastUpdated", ...} -> {JobsCount, Jobs{Job[{Link,
Questions[{QuestionName, Value}]}]}} 50 per page; the Questions carry
jobtitle, jobdescription (full HTML), lastupdated and site-specific
formtext fields. Same flow as scrape.fetch_brassring, with the college
title filter and location picked from any *location*/city/campus question.
Verified against Texas Tech University System (25898/5637) and Osceola
County School District (26623/5686, 189 jobs).
"""
import html as _html
import re
from datetime import datetime

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "brassring"
needs_browser = False
URL_RE = re.compile(r"https?://[a-z0-9.-]*brassring\.com/[^\s\"'<>]*partnerid=(\d+)[^\s\"'<>]*siteid=(\d+)", re.I)
SEARCH = "https://sjobs.brassring.com/TGnewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
MAX_PAGES = 40


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search((html or "").replace("&amp;", "&"))
    if not m:
        return None
    pid, sid = m.group(1), m.group(2)
    return {"url": f"https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid={pid}&siteid={sid}",
            "partnerid": pid, "siteid": sid}


def _date(s):
    for fmt in ("%d-%b-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean(s), fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def fetch(config):
    try:
        pid, sid = config["partnerid"], config["siteid"]
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get(f"https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid={pid}&siteid={sid}", timeout=30)
        out, seen, total, fetched = [], set(), None, 0
        for page in range(1, MAX_PAGES + 1):
            body = {"partnerId": pid, "siteId": sid, "keyword": "", "location": "", "turnOffHttps": False,
                    "facetfilterfields": {"Facet": []}, "powersearchoptions": {"PowerSearchOption": []},
                    "SortType": "LastUpdated", "pageNumber": page, "encryptedSessionValue": ""}
            r = s.post(SEARCH, json=body, headers={"Accept": "application/json"}, timeout=30)
            if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                break
            d = r.json()
            jobs = (d.get("Jobs") or {}).get("Job") or []
            total = total or int(d.get("JobsCount") or 0)
            fetched += len(jobs)
            for j in jobs:
                qs = {str(q.get("QuestionName") or "").lower(): q.get("Value") for q in (j.get("Questions") or [])}
                jid = str(qs.get("reqid") or qs.get("autoreq") or "")
                title = clean(_html.unescape(str(qs.get("jobtitle") or qs.get("title") or "")))
                if not jid or jid in seen or not title_ok(title):
                    continue
                seen.add(jid)
                loc = next((clean(str(v)) for k, v in qs.items() if v and re.search(r"location|city|campus", k)), "")
                url = (j.get("Link") or "").replace("\\/", "/") or \
                    f"https://sjobs.brassring.com/TGnewUI/Search/Home/HomeWithPreLoad?PageType=JobDetails&partnerid={pid}&siteid={sid}&jobId={jid}"
                out.append(make_job(config, "college_brassring", title, url, location=loc,
                                    description=strip_html(str(qs.get("jobdescription") or qs.get("description") or ""))[:20000],
                                    posted=_date(str(qs.get("lastupdated") or ""))))
            if not jobs or fetched >= total:
                break
        return out
    except Exception:
        return []
