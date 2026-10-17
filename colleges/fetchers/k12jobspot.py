"""K12JobSpot national search (Frontline/PowerSchool AppliTrack districts).

The Next.js site at /en/job-postings?q=<q> reads a public JSON API:
  GET https://www.k12jobspot.com/api/job-postings?q=<q>&skip=N&limit=50
    -> {"total", "data": [{id, title, description (markdown), externalOpenedAt,
        rhApplicationUrl, hiringOrganization{name, address{city,stateProvinceCode}},
        jobLocations[{address}], positionAreas[], baseSalary{minValue,maxValue}}]}
No detail call is needed. Postings link to /en/job-postings/<id>.
"""
import time

import requests

from ._common import HEADERS, make_job, title_ok, clean

name = "k12jobspot"
needs_browser = False
BASE = "https://www.k12jobspot.com"
QUERIES = ["adjunct", "computer science", "instructor computer", "cybersecurity",
           "information technology", "college instructor", "programming instructor"]
LIMIT = 50
MAX_PAGES = 10
DELAY = 1.0
_H = dict(HEADERS, Accept="application/json")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _loc(j):
    for src in (j.get("jobLocations") or []) + [j.get("hiringOrganization") or {}]:
        a = (src or {}).get("address") or {}
        parts = [clean(a.get("city") or ""), clean(a.get("stateProvinceCode") or "")]
        if any(parts):
            return ", ".join(p for p in parts if p)
    return ""


def fetch(config):
    try:
        seen, out = set(), []
        for q in QUERIES:
            for page in range(MAX_PAGES):
                try:
                    r = requests.get(f"{BASE}/api/job-postings", params={"q": q, "skip": page * LIMIT, "limit": LIMIT},
                                     headers=_H, timeout=30)
                except Exception:
                    break
                if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                    break
                j = r.json()
                rows = j.get("data") or []
                for it in rows:
                    jid, title = it.get("id"), clean(it.get("title") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    if not title_ok(title):
                        continue
                    org = (it.get("hiringOrganization") or {}).get("name") or ""
                    url = f"{BASE}/en/job-postings/{jid}"
                    sal = it.get("baseSalary") or {}
                    meta = []
                    if it.get("positionAreas"):
                        meta.append("Areas: " + ", ".join(it["positionAreas"]))
                    if it.get("employmentType"):
                        meta.append(f"Employment type: {it['employmentType']}")
                    if sal.get("maxValue"):
                        meta.append(f"Salary: ${sal.get('minValue', 0):,.0f} - ${sal['maxValue']:,.0f}")
                    desc = "\n".join(meta) + "\n\n" + (it.get("description") or "")
                    cfg = dict(config, name=clean(org) or config.get("name", ""))
                    try:
                        out.append(make_job(cfg, "college_k12jobspot", title, url, _loc(it), desc.strip(),
                                            (it.get("externalOpenedAt") or "")[:10]))
                    except Exception:
                        continue
                time.sleep(DELAY)
                if len(rows) < LIMIT or (page + 1) * LIMIT >= int(j.get("total") or 0):
                    break
        return out
    except Exception:
        return []
