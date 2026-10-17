"""ADP Recruitment Management career sites on myjobs.adp.com (no WorkforceNow
cid), also where the legacy recruiting.adp.com/srccar/public/RTI.home?c=<id>
links now redirect:
  https://myjobs.adp.com/<domain>/cx

config: {"url": ..., "domain": "<domain>"}
GET myjobs.adp.com/public/staffing/v1/career-site/<domain> gives orgoid and
myJobsToken; with those as headers (plus rolecode=manager),
my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters
pages the reqs ($top/$skip) with jobDescription inline.
Verified against College of Central Florida and MCPHS (via recruiting.adp.com).
"""
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "myjobsadp"
needs_browser = False
LIST = ("https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters"
        "?$orderby=postingDate%20desc&$select=reqId,jobTitle,publishedJobTitle,type,jobDescription,"
        "jobQualifications,workLocations,workLevelCode,clientRequisitionID,postingDate,requisitionLocations"
        "&$top={top}&$skip={skip}&tz=America/New_York")
PAGE, MAX_JOBS = 50, 1500

DOMAIN_RE = re.compile(r"https?://myjobs\.adp\.com/([a-z0-9_-]+)", re.I)
LEGACY_RE = re.compile(r"https?://recruiting\.adp\.com/srccar/public/RTI\.home\?[^\"'\s<>]*c=\d+[^\"'\s<>]*", re.I)


def detect(url, html=""):
    m = DOMAIN_RE.search(url or "") or DOMAIN_RE.search(html or "")
    if not m:
        # legacy RTI.home links redirect to the myjobs domain
        lm = LEGACY_RE.search(url or "") or LEGACY_RE.search((html or "").replace("&amp;", "&"))
        if not lm:
            return None
        try:
            r = requests.get(lm.group(0).replace("&amp;", "&"), headers=HEADERS, timeout=20, allow_redirects=True)
            m = DOMAIN_RE.search(r.url)
        except Exception:
            m = None
        if not m:
            return None
    dom = m.group(1).lower()
    if dom in ("public", "cx", "assets"):
        return None
    return {"url": f"https://myjobs.adp.com/{dom}/cx", "domain": dom}


def fetch(config):
    try:
        dom = config.get("domain") or (DOMAIN_RE.search(config["url"]) or [None, ""])[1]
        r = requests.get(f"https://myjobs.adp.com/public/staffing/v1/career-site/{dom}",
                         headers=dict(HEADERS, Accept="application/json"), timeout=25)
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            return []
        cs = r.json()
        # The list endpoint answers count=0 when an Accept-Language header is sent.
        hdrs = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json",
                "orgoid": cs.get("orgoid") or "", "myjobstoken": cs.get("myJobsToken") or "",
                "rolecode": "manager"}
        out, seen, skip = [], set(), 0
        while skip < MAX_JOBS:
            r = requests.get(LIST.format(top=PAGE, skip=skip), headers=hdrs, timeout=25)
            if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                break
            d = r.json()
            reqs = d.get("jobRequisitions") or []
            for j in reqs:
                title = clean(j.get("publishedJobTitle") or j.get("jobTitle") or "")
                rid = j.get("reqId") or ""
                if not title or not rid or rid in seen or not title_ok(title):
                    continue
                seen.add(rid)
                locs = []
                for L in j.get("requisitionLocations") or []:
                    ad = L.get("address") or {}
                    s = ", ".join(x for x in (ad.get("cityName"), (ad.get("countrySubdivisionLevel1") or {}).get("codeValue")) if x)
                    if s and s not in locs:
                        locs.append(s)
                out.append(make_job(config, "college_myjobsadp", title,
                                    f"https://myjobs.adp.com/{dom}/cx/job-listing/{rid}",
                                    location="; ".join(locs),
                                    description=strip_html(" ".join(j.get(k) or "" for k in ("jobDescription", "jobQualifications")))[:20000],
                                    posted=(j.get("postingDate") or "")[:10]))
            skip += len(reqs)
            if not reqs or skip >= (d.get("count") or 0):
                break
        return out
    except Exception:
        return []
