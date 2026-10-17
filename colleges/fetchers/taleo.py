"""Oracle Taleo Enterprise career sections:
https://<tenant>.taleo.net/careersection/<section>/jobsearch.ftl

The search page is a JS app, but it is backed by a JSON endpoint:
  POST /careersection/rest/jobboard/searchjobs?lang=en&portal=<portalNo>
with the body the page itself sends (KEYWORD field + empty facet filters).
The portal number is embedded in jobsearch.ftl ("portalNo: '101430233'").
Detail pages (jobdetail.ftl?job=<jobId>&lang=en) carry the whole requisition
in a hidden <input id="initialHistory"> as a URL-encoded "!|!"-separated
blob; the description is the first element that starts with "!*!".
Verified against Stony Brook (stonybrooku / section 2).
"""
import re
from urllib.parse import urlsplit, unquote
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "taleo"
needs_browser = False
QUERIES = ["adjunct", "instructor", "lecturer", "faculty", "professor"]
MAX_JOBS = 1000
# The endpoint 500s ("An Error Occurred in TEE") unless the browser's tz headers are present.
JSON_HEADERS = dict(HEADERS, Accept="application/json", tz="GMT-05:00", tzname="America/New_York")
_FILTERS = ["POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE"]
_ADV = ["ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB", "EMPLOYEE_STATUS",
        "STUDY_LEVEL", "WILL_TRAVEL", "JOB_SHIFT", "JOB_LEVEL"]


def detect(url, html=""):
    p = urlsplit(url)
    m = re.search(r"/careersection/([^/?#]+)/", p.path)
    if p.netloc.lower().endswith("taleo.net") and m:
        return {"url": url, "tenant": p.netloc.split(".")[0], "section": m.group(1)}
    m2 = re.search(r"https?://([a-z0-9-]+)\.taleo\.net/careersection/([^/?#\"']+)/", html or "", re.I)
    if m2:
        return {"url": url, "tenant": m2.group(1), "section": m2.group(2)}
    return None


def _base(config):
    return f"https://{config['tenant']}.taleo.net/careersection/{config['section']}"


def _portal(config):
    if config.get("portal"):
        return config["portal"]
    try:
        r = requests.get(_base(config) + "/jobsearch.ftl", params={"lang": "en"},
                         headers=HEADERS, timeout=25)
        m = re.search(r"portalNo:\s*'(\d+)'", r.text) or re.search(r"portal=(\d+)", r.text)
        if m:
            config["portal"] = m.group(1)
            return m.group(1)
    except Exception:
        pass
    return ""


def _search(config, portal, keyword):
    out, page = [], 1
    while len(out) < MAX_JOBS:
        body = {"multilineEnabled": False,
                "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": [{"id": f, "selectedValues": []} for f in _FILTERS]},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [{"id": f, "selectedValues": []} for f in _ADV]},
                "pageNo": page}
        try:
            r = requests.post(_base(config).replace(f"/careersection/{config['section']}", "/careersection/rest/jobboard/searchjobs"),
                              params={"lang": "en", "portal": portal}, json=body,
                              headers=JSON_HEADERS, timeout=30)
            d = r.json()
        except Exception:
            break
        reqs = d.get("requisitionList") or []
        out.extend(reqs)
        pg = d.get("pagingData") or {}
        total = int(pg.get("totalCount") or 0)
        size = int(pg.get("pageSize") or 25)
        if not reqs or page * size >= total:
            break
        page += 1
    return out


def _parse_date(s):
    s = clean(s).replace("\\", "")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M %p"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _detail(config, job_id):
    try:
        r = requests.get(_base(config) + "/jobdetail.ftl", params={"job": job_id, "lang": "en"},
                         headers=HEADERS, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        inp = soup.find("input", id="initialHistory")
        if not inp or not inp.get("value"):
            return "", ""
        parts = unquote(inp["value"]).split("!|!")
        desc = ""
        for p in parts:
            if p.startswith("!*!"):
                desc = strip_html(p[3:].replace("\\:", ":"))
                break
        posted = ""
        for p in parts:
            if re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", clean(p)):
                posted = _parse_date(p)
                break
        return desc, posted
    except Exception:
        return "", ""


def fetch(config):
    try:
        if not config.get("tenant") or not config.get("section"):
            cfg = detect(config.get("url", ""), "")
            if not cfg:
                return []
            config = dict(config, **cfg)
        portal = _portal(config)
        if not portal:
            return []
        out, seen = [], set()
        for q in QUERIES:
            for r in _search(config, portal, q):
                jid = str(r.get("jobId") or "")
                cols = r.get("column") or []
                title = clean(cols[0]) if cols else ""
                if not jid or jid in seen or not title_ok(title):
                    continue
                seen.add(jid)
                loc = ""
                for i in (r.get("locationsColumns") or []):
                    if i < len(cols):
                        loc = clean(re.sub(r'[\[\]"]', "", cols[i]).replace("US-", ""))
                        break
                url = f"{_base(config)}/jobdetail.ftl?job={jid}&lang=en"
                desc, posted = _detail(config, jid)
                out.append(make_job(config, "college_taleo", title, url, loc, desc, posted))
                if len(out) >= MAX_JOBS:
                    return out
        return out
    except Exception:
        return []
