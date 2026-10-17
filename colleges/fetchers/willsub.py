"""ESS WillSub+ district boards (substitute and support staffing for school
districts and vocational-technical schools): https://jobs.willsubplus.com/jobs?districtIds=<id>

config: {"url": "https://jobs.willsubplus.com/jobs?districtIds=<id>", "district_ids": "<id>"}
The Next.js page embeds its data in <script id="__NEXT_DATA__">:
props.pageProps.jobPostingsList[{id, positionTitle, jobPostingType,
description (HTML), city, stateCode, districtTitle, payType, minPayRate,
maxPayRate, updatedAt}] and jobPostingsListMeta{totalPages, size=100};
&page=N pages. The posting page is /jobs/<slug>-<id> (links in the HTML).
Verified against Ocean County Vocational Technical School (2804, 4 postings).
"""
import json
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "willsub"
needs_browser = False
URL_RE = re.compile(r"https?://jobs\.willsubplus\.com/jobs\?[^\s\"'<>]*districtIds=([\d,]+)", re.I)
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
MAX_PAGES = 20


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search((html or "").replace("&amp;", "&"))
    if not m:
        return None
    return {"url": f"https://jobs.willsubplus.com/jobs?districtIds={m.group(1)}", "district_ids": m.group(1)}


def fetch(config):
    try:
        ids = config.get("district_ids") or (URL_RE.search(config["url"]) or [None, ""])[1]
        out, seen = [], set()
        for page in range(0, MAX_PAGES):
            r = requests.get("https://jobs.willsubplus.com/jobs", params={"districtIds": ids, "page": page},
                             headers=HEADERS, timeout=30)
            m = NEXT_RE.search(r.text) if r.status_code == 200 else None
            if not m:
                break
            pp = (json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}
            rows = pp.get("jobPostingsList") or []
            links = dict(re.findall(r'href="(/jobs/[^"]*-(\d+))"', r.text))
            links = {jid: path for path, jid in links.items()}
            for j in rows:
                jid = str(j.get("id") or "")
                title = clean(j.get("positionTitle") or "")
                if not jid or jid in seen or not title_ok(title):
                    continue
                seen.add(jid)
                loc = ", ".join(x for x in (clean(j.get("city") or ""), clean(j.get("stateCode") or "")) if x)
                body = j.get("description") or ""
                if j.get("minPayRate") or j.get("maxPayRate"):
                    body = f"Pay: {j.get('minPayRate') or ''} - {j.get('maxPayRate') or ''} {j.get('payType') or ''}\n{body}"
                url = "https://jobs.willsubplus.com" + links.get(jid, f"/jobs?districtIds={ids}")
                out.append(make_job(config, "college_willsub", title, url, location=loc,
                                    description=strip_html(body)[:20000],
                                    posted=str(j.get("createdAt") or j.get("updatedAt") or "")[:10],
                                    department=clean(j.get("districtTitle") or "")))
            meta = pp.get("jobPostingsListMeta") or {}
            if meta.get("last", True) or page + 1 >= int(meta.get("totalPages") or 1):
                break
        return out
    except Exception:
        return []
