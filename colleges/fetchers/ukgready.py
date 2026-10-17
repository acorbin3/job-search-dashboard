"""UKG Ready (ex-Kronos Workforce Ready) career pages:
  https://secureN.saashr.com/ta/<company>.careers?CareersSearch
  https://secureN.entertimeonline.com/ta/<company>.careers?CareersSearch   (CBIZ-hosted)
  (also *.onehcm.com and *.yourpayrollhr.com reseller hosts)

config: {"url": ..., "host": "secure6.saashr.com", "company": "6000630"}
The page is a JS app, but the JSON it calls is public:
  /ta/rest/ui/recruitment/companies/|<company>/job-requisitions?offset=1&size=20&sort=desc&lang=en-US
    -> {"job_requisitions": [...], "_paging": {"offset","size","total"}}
  /ta/rest/ui/recruitment/companies/|<company>/job-requisitions/<id>?lang=en-US
    -> full job_description HTML.
Verified against Pace and Point University (saashr) and Crown College (entertimeonline).
"""
import re
from urllib.parse import urlsplit

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "ukgready"
needs_browser = False
PAGE = 50
MAX_JOBS = 1500

URL_RE = re.compile(r"https?://([a-z0-9.-]+\.(?:saashr|entertimeonline|onehcm|yourpayrollhr)\.com)/ta/([A-Za-z0-9_-]+)\.careers", re.I)


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search(html or "")
    if not m:
        return None
    host, company = m.group(1).lower(), m.group(2)
    return {"url": f"https://{host}/ta/{company}.careers?CareersSearch", "host": host, "company": company}


def _api(config):
    host = config.get("host") or urlsplit(config["url"]).netloc
    company = config.get("company") or (URL_RE.search(config["url"]) or [None, None, ""])[2]
    return f"https://{host}/ta/rest/ui/recruitment/companies/%7C{company}/job-requisitions", host, company


def _detail(base, jid):
    try:
        r = requests.get(f"{base}/{jid}", params={"lang": "en-US"},
                         headers=dict(HEADERS, Accept="application/json"), timeout=20)
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            return ""
        d = r.json()
        return strip_html(" ".join(d.get(k) or "" for k in ("job_preview", "job_description", "job_requirement")))[:20000]
    except Exception:
        return ""


def fetch(config):
    try:
        base, host, company = _api(config)
        out, seen, offset = [], set(), 1
        while offset <= MAX_JOBS:
            r = requests.get(base, params={"offset": offset, "size": PAGE, "sort": "desc", "ein_id": "", "lang": "en-US"},
                             headers=dict(HEADERS, Accept="application/json"), timeout=25)
            if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                break
            d = r.json()
            reqs = d.get("job_requisitions") or []
            for j in reqs:
                title = clean(j.get("job_title") or "")
                jid = j.get("id")
                if not title or not jid or jid in seen or not title_ok(title):
                    continue
                seen.add(jid)
                L = j.get("location") or {}
                loc = ", ".join(x for x in (L.get("city"), L.get("state")) if x)
                href = f"https://{host}/ta/{company}.careers?CareersSearch&show_req={jid}&lang=en-US"
                out.append(make_job(config, "college_ukgready", title, href, location=loc,
                                    description=_detail(base, jid),
                                    department=", ".join(j.get("job_categories") or [])))
            total = (d.get("_paging") or {}).get("total") or 0
            offset += len(reqs)
            if not reqs or offset > total:
                break
        return out
    except Exception:
        return []
