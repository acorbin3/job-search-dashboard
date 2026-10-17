"""Infor CloudSuite HCM (Global HR) job boards:
  https://<tenant>.inforcloudsuite.com/hcm/Jobs/page/JobsHomePage?csk.JobBoard=<board>&csk.HROrganization=<org>

config: {"url", "host": "<tenant>.inforcloudsuite.com", "job_board": "...", "hr_org": "..."}
The Landmark UI is a JS shell; after a session GET of the home page (SSO
redirect sets the cookies) the list is
GET /hcm/Jobs/list/JobPosting.SearchForJobsResults?pageop=load&pagesize=500
&pagepanel=JobsHomePage.Jobs.NewJobsForAnonymous&csk.* -> dataViewSet.data[
{resourceId "JobPosting[JobPostingSet](<org>,<reqid>,<n>)", fields{Description,
Category, LocationOfJob_prd_Location_ohb_3_chb_ (city) ..., PostingDateRange_prd_Begin}}]
and the body is GET /hcm/Jobs/form/<resourceId>.JobPostingDisplay?pageop=load
&pagesize=1 -> fields._op_PositionDescription_spc_translation_cp_ (HTML).
Verified against Hillsborough County Public Schools (477 postings).
"""
import re
from urllib.parse import quote, urlsplit, parse_qs, urlencode

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "infor"
needs_browser = False
URL_RE = re.compile(r"https?://([a-z0-9.-]+\.inforcloudsuite\.com)(?::\d+)?/hcm/Jobs/[^\s\"'<>]*", re.I)
JSON_HEADERS = {"Accept": "application/json, text/plain, */*"}


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search((html or "").replace("&amp;", "&"))
    if not m:
        return None
    q = parse_qs(urlsplit(m.group(0)).query)
    board, org = q.get("csk.JobBoard", [""])[0], q.get("csk.HROrganization", [""])[0]
    if not org:
        return None
    host = m.group(1).lower()
    return {"url": f"https://{host}/hcm/Jobs/page/JobsHomePage?" + urlencode({"csk.JobBoard": board, "csk.HROrganization": org}),
            "host": host, "job_board": board, "hr_org": org}


def _val(fields, key):
    return clean(str((fields.get(key) or {}).get("value") or ""))


def fetch(config):
    try:
        host, board, org = config["host"], config.get("job_board", ""), config["hr_org"]
        csk = urlencode({"csk.JobBoard": board, "csk.HROrganization": org})
        base = f"https://{host}/hcm/Jobs"
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get(f"{base}/page/JobsHomePage?{csk}", timeout=40)
        r = s.get(f"{base}/list/JobPosting.SearchForJobsResults?pageop=load&pagesize=500"
                  f"&pagepanel=JobsHomePage.Jobs.NewJobsForAnonymous&{csk}", headers=JSON_HEADERS, timeout=60)
        if r.status_code != 200 or not r.text.startswith("{"):
            return []
        out, seen = [], set()
        for row in (r.json().get("dataViewSet") or {}).get("data") or []:
            f, rid = row.get("fields") or {}, row.get("resourceId") or ""
            title = _val(f, "Description")
            if not rid or rid in seen or not title_ok(title):
                continue
            seen.add(rid)
            body = ""
            try:
                rd = s.get(f"{base}/form/{quote(rid)}.JobPostingDisplay?pageop=load&pagesize=1"
                           f"&navigation={quote(rid)}.JobPostingDisplayNav&{csk}", headers=JSON_HEADERS, timeout=40)
                df = (rd.json() if rd.status_code == 200 else {}).get("fields") or {}
                body = df.get("_op_PositionDescription_spc_translation_cp_", {}).get("value") or ""
                pay = _val(df, "_op_FormattedSalaryRangeAmountWithCurrencyCodeAndPayRate_spc_translation_cp_")
                if pay and not pay.startswith("0 - 0"):
                    body = f"Salary: {pay}\n{body}"
            except Exception:
                pass
            loc = ", ".join(x for x in (_val(f, "LocationOfJob_prd_Location_ohb_4_chb_"), _val(f, "LocationOfJob_prd_Location_ohb_3_chb_"),
                                        _val(f, "LocationOfJob_prd_Location_ohb_2_chb_")) if x) or _val(f, "LocationOfJob")
            d = _val(f, "PostingDateRange_prd_Begin")
            posted = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else ""
            out.append(make_job(config, "college_infor", title, f"{base}/form/{quote(rid)}.JobPostingDisplay?{csk}",
                                location=loc, description=strip_html(body)[:20000], posted=posted,
                                department=_val(f, "_op_Category_prd_Description_spc_translation_cp_") or _val(f, "Category")))
        return out
    except Exception:
        return []
