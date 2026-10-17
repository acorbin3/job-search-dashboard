"""Keldair job boards: https://jobs.keldair.com/<instance>[/<subBrand>]
(also the white labels jobs.citysuitehr.com, jobs.connect-ats.com,
jobs.keldairaps.com, jobs.constructhire.com, jobs.strongpayhire.com).

config: {"url", "host": "jobs.keldair.com", "instance": "<instance>", "sub_brand": ""}
The Angular app calls https://job-gateway.<domain>/jobs?pageNumber=N
&sort.field=PostedDate&sort.direction=1&visibilityType=External
[&subBrandIdentifier=<subBrand>] with the header X-InstanceIdentifier: <instance>
(without it the gateway 500s) -> jobs{pageCount, results[{id, title,
locationCityProvince, departmentName, postedDate, remoteStatus}]}; the body is
GET /jobs/<id> -> jobDescriptions[].description, employmentType, location.
Verified against University of Indianapolis (uindy, 29 jobs).
"""
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "keldair"
needs_browser = False
HOST_RE = re.compile(r"https?://(jobs\.(?:keldair|citysuitehr|connect-ats|keldairaps|constructhire|strongpayhire)\.com)"
                     r"/([a-z0-9-]+)(?:/([a-z0-9-]+))?", re.I)
RESERVED = {"jobs", "internal", "api", "dist", "assets"}
MAX_PAGES = 50


def detect(url, html=""):
    m = HOST_RE.search(url or "") or HOST_RE.search(html or "")
    if not m or m.group(2).lower() in RESERVED:
        return None
    host, inst = m.group(1).lower(), m.group(2)
    sub = m.group(3) or ""
    if sub.lower() in RESERVED:
        sub = ""
    return {"url": f"https://{host}/{inst}" + (f"/{sub}" if sub else ""), "host": host,
            "instance": inst, "sub_brand": sub}


def _headers(host, inst):
    return dict(HEADERS, Accept="application/json, text/plain, */*", Origin=f"https://{host}",
                Referer=f"https://{host}/{inst}", **{"X-InstanceIdentifier": inst})


def _detail(gw, jid, hdrs):
    try:
        r = requests.get(f"{gw}/jobs/{jid}", headers=hdrs, timeout=25)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def fetch(config):
    try:
        host, inst, sub = config["host"], config["instance"], config.get("sub_brand") or ""
        gw = "https://job-gateway." + host.split(".", 1)[1]
        hdrs = _headers(host, inst)
        base = f"https://{host}/{inst}" + (f"/{sub}" if sub else "")
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            params = {"pageNumber": page, "sort.field": "PostedDate", "sort.direction": 1,
                      "visibilityType": "External"}
            if sub:
                params["subBrandIdentifier"] = sub
            r = requests.get(f"{gw}/jobs", params=params, headers=hdrs, timeout=30)
            if r.status_code != 200:
                break
            jobs = (r.json() or {}).get("jobs") or {}
            rows = jobs.get("results") or []
            for j in rows:
                jid = str(j.get("id") or "")
                title = clean(j.get("title") or "")
                if not jid or jid in seen or not title_ok(title):
                    continue
                seen.add(jid)
                d = _detail(gw, jid, hdrs)
                body = "\n".join(x.get("description") or "" for x in (d.get("jobDescriptions") or []))
                if d.get("employmentType"):
                    body = f"Employment type: {d['employmentType']}\n{body}"
                loc = clean(j.get("locationCityProvince") or d.get("location") or "")
                if (j.get("remoteStatus") or "None") != "None" and "remote" not in loc.lower():
                    loc = f"{loc} (Remote: {j['remoteStatus']})".strip()
                out.append(make_job(config, "college_keldair", title, f"{base}/jobs/{jid}", location=loc,
                                    description=strip_html(body)[:20000],
                                    posted=(j.get("postedDate") or "")[:10],
                                    department=clean(j.get("departmentName") or "")))
            if not rows or page >= int(jobs.get("pageCount") or 1):
                break
        return out
    except Exception:
        return []
