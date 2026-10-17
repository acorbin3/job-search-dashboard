"""Oracle HCM Cloud Recruiting (CandidateExperience) boards:
https://<host>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/en/sites/<Site>/requisitions

The Angular UI talks to a public REST API that needs no auth:
  GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
      ?onlyData=true&expand=requisitionList.secondaryLocations,flexFieldsFacet.values
      &finder=findReqs;siteNumber=<Site>,keyword=<kw>,limit=100,offset=0
  GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
      ?expand=all&onlyData=true&finder=ById;Id="<reqId>",siteNumber=<Site>
Verified against Vanderbilt (ecsr.fa.us2 / CX_1) and Moravian (ibtsjb.fa.ocs / CX_2).
"""
import re
from urllib.parse import urlsplit

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "oracle"
needs_browser = False
QUERIES = ["adjunct", "instructor", "lecturer", "faculty", "professor"]
MAX_JOBS = 1000
API = "/hcmRestApi/resources/latest/"
JSON_HEADERS = dict(HEADERS, Accept="application/json")


def detect(url, html=""):
    p = urlsplit(url)
    m = re.search(r"/hcmUI/CandidateExperience/(?:[a-z-]+/)?sites/([^/?#]+)", url, re.I)
    if re.search(r"oracle(?:gov)?cloud\d*\.com$", p.netloc.lower()) and m:
        return {"url": url, "host": f"{p.scheme}://{p.netloc}", "site": m.group(1)}
    # Some schools proxy via a vanity host; the HTML still names the site.
    m2 = re.search(r"https?://([^/\"']+oracle(?:gov)?cloud\d*\.com)/hcmUI/CandidateExperience/[a-z-]+/sites/([^/?#\"']+)",
                   html or "", re.I)
    if m2:
        return {"url": url, "host": f"https://{m2.group(1)}", "site": m2.group(2)}
    return None


def _job_url(config, req_id):
    lang = "en"
    m = re.search(r"/CandidateExperience/([a-z-]+)/sites/", config.get("url", ""), re.I)
    if m:
        lang = m.group(1)
    return f"{config['host']}/hcmUI/CandidateExperience/{lang}/sites/{config['site']}/job/{req_id}"


def _detail(config, req_id):
    try:
        finder = f'ById;Id="{req_id}",siteNumber={config["site"]}'
        r = requests.get(config["host"] + API + "recruitingCEJobRequisitionDetails",
                         params={"expand": "all", "onlyData": "true", "finder": finder},
                         headers=JSON_HEADERS, timeout=25)
        items = r.json().get("items") or []
        if not items:
            return {}
        it = items[0]
        parts = [it.get("ExternalDescriptionStr"), it.get("ExternalResponsibilitiesStr"),
                 it.get("ExternalQualificationsStr")]
        desc = strip_html("\n".join(p for p in parts if p))
        posted = (it.get("ExternalPostedStartDate") or "")[:10]
        return {"description": desc, "posted": posted,
                "location": clean(it.get("PrimaryLocation") or ""),
                "department": clean(it.get("Department") or it.get("Organization") or "")}
    except Exception:
        return {}


def _search(config, keyword):
    base = config["host"] + API + "recruitingCEJobRequisitions"
    offset, out = 0, []
    while offset < MAX_JOBS:
        finder = f"findReqs;siteNumber={config['site']},keyword={keyword},limit=100,offset={offset}"
        try:
            r = requests.get(base, params={
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations,flexFieldsFacet.values",
                "finder": finder}, headers=JSON_HEADERS, timeout=30)
            items = r.json().get("items") or []
        except Exception:
            break
        if not items:
            break
        reqs = items[0].get("requisitionList") or []
        out.extend(reqs)
        total = int(items[0].get("TotalJobsCount") or 0)
        offset += len(reqs)
        if not reqs or offset >= total:
            break
    return out


def fetch(config):
    try:
        if not config.get("host") or not config.get("site"):
            cfg = detect(config.get("url", ""), "")
            if not cfg:
                return []
            config = dict(config, **cfg)
        out, seen = [], set()
        for q in QUERIES:
            for r in _search(config, q):
                rid = str(r.get("Id") or "")
                title = clean(r.get("Title") or "")
                if not rid or rid in seen or not title_ok(title):
                    continue
                seen.add(rid)
                d = _detail(config, rid)
                loc = d.get("location") or clean(r.get("PrimaryLocation") or "")
                desc = d.get("description") or strip_html(r.get("ShortDescriptionStr") or "")
                posted = d.get("posted") or (r.get("PostedDate") or "")[:10]
                out.append(make_job(config, "college_oracle", title, _job_url(config, rid),
                                    loc, desc, posted, d.get("department", "")))
                if len(out) >= MAX_JOBS:
                    return out
        return out
    except Exception:
        return []
