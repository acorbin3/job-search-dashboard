"""Interfolio Faculty Search.

Per-institution public boards live at https://apply.interfolio.com/<tenant>/positions
(e.g. Dartmouth 11002, Hillsdale 24461); single postings at
https://apply.interfolio.com/<positionId>. The Angular app reads the board from
  GET https://logic.interfolio.com/byc-search/<tenant>/public_job_boards
      ?search=<kw>&page=1&limit=100&sort_by=open_date&sort_order=desc
  -> {"results":[...], "total_count": N, "title": "...", "unit_list": [...]}
and a single position from .../public_job_boards/<positionId>.

Interfolio also builds faculty career sites on a school host (Vue app with
/assets/uisvue-*.js), e.g. https://facultyjobs.jhu.edu/positions, which expose
  GET <site>/api/positions?page=N&pageSize=100
  -> {"result": {"positions": [...], "total", "totalPages", "page"}}
with description / qualifications / location / openDate / unitName inline and
detail pages at <site>/positions/details/<id>; config {"url", "site"}.

Config: {"url", "tenant"}. If the tenant id is unknown (a school only links
individual apply.interfolio.com/<id> pages), set config["position_urls"] to a
list of those URLs and fetch() will scrape each one via the single-position
endpoint instead.

logic.interfolio.com sits behind Cloudflare rate limiting (HTTP 429 / error
1015) that kicks in after a burst of requests, so calls are paced and retried.
"""
import re
import time
from urllib.parse import urlsplit

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "interfolio"
needs_browser = False
QUERIES = ["adjunct", "instructor", "lecturer", "faculty", "professor"]
MAX_JOBS = 1000
API = "https://logic.interfolio.com/byc-search"
JSON_HEADERS = dict(HEADERS, Accept="application/json")
PAUSE = 1.0


def detect(url, html=""):
    p = urlsplit(url)
    if "/assets/uisvue-" in (html or "") and "interfolio.com" not in p.netloc.lower():
        site = f"{p.scheme}://{p.netloc}"
        try:
            d = requests.get(f"{site}/api/positions", params={"page": 1}, headers=JSON_HEADERS, timeout=15).json()
            if isinstance((d.get("result") or {}).get("positions"), list):
                return {"url": f"{site}/positions", "site": site, "tenant": ""}
        except Exception:
            pass
    if "interfolio.com" in p.netloc.lower():
        m = re.search(r"/(\d+)/positions", p.path)
        return {"url": url, "tenant": m.group(1) if m else ""}
    m = re.search(r"https?://apply\.interfolio\.com/(\d+)/positions", html or "")
    if m:
        return {"url": m.group(0), "tenant": m.group(1)}
    return None


def _get(url, params=None):
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=JSON_HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(30 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None
    return None


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return ""


def _fields(d):
    parts = [_first(d, "description", "position_description"), _first(d, "qualifications"),
             _first(d, "instructions", "application_instructions")]
    return {
        "title": clean(_first(d, "name", "title", "position_title")),
        "description": strip_html("\n".join(p for p in parts if p)),
        "location": clean(_first(d, "location", "city")),
        "posted": str(_first(d, "open_date_raw", "open_date", "created_at"))[:10],
        "department": clean(_first(d, "unit_name", "department", "unit")),
    }


def _position(pid, tenant=""):
    d = _get(f"{API}/{tenant}/public_job_boards/{pid}" if tenant else f"{API}/public_job_boards/{pid}")
    if not isinstance(d, dict):
        return {}
    return _fields(d.get("position") or d.get("result") or d)


def _board(tenant, keyword):
    out, page = [], 1
    while len(out) < MAX_JOBS:
        d = _get(f"{API}/{tenant}/public_job_boards",
                 {"search": keyword, "page": page, "limit": 100,
                  "sort_by": "open_date", "sort_order": "desc"})
        if not isinstance(d, dict):
            break
        rows = d.get("results") or []
        out.extend(rows)
        total = int(d.get("total_count") or 0)
        if not rows or len(out) >= total:
            break
        page += 1
        time.sleep(PAUSE)
    return out


def _site(config):
    site = config["site"].rstrip("/")
    out, seen, page = [], set(), 1
    while page <= 50:
        d = _get(f"{site}/api/positions", {"page": page, "pageSize": 100})
        res = (d or {}).get("result") or {}
        rows = res.get("positions") or []
        for r in rows:
            pid = str(r.get("id") or "")
            title = clean(r.get("name") or "")
            if not pid or pid in seen or r.get("archived") or not title_ok(title):
                continue
            seen.add(pid)
            loc = r.get("location") or {}
            parts = [_first(r, "description"), _first(r, "qualifications"), _first(r, "instructions")]
            if r.get("salaryRange"):
                parts.insert(0, f"Salary: {r['salaryRange']}")
            out.append(make_job(config, "college_interfolio", title, f"{site}/positions/details/{pid}",
                                clean(", ".join(x for x in (loc.get("city"), loc.get("state")) if x)),
                                strip_html("\n".join(p for p in parts if p)),
                                str(_first(r, "openDate", "createdAt"))[:10], clean(_first(r, "unitName", "department"))))
        if not rows or page >= int(res.get("totalPages") or 1):
            break
        page += 1
        time.sleep(PAUSE)
    return out


def fetch(config):
    try:
        if config.get("site"):
            return _site(config)
        tenant = str(config.get("tenant") or "")
        out, seen = [], set()
        if tenant:
            # Board rows already carry description/qualifications/location/open_date_raw,
            # so no per-position call is needed (which also keeps Cloudflare quiet).
            for q in QUERIES:
                for r in _board(tenant, q):
                    pid = str(_first(r, "id", "position_id"))
                    title = clean(_first(r, "name", "title", "position_title"))
                    if not pid or pid in seen or not title_ok(title):
                        continue
                    seen.add(pid)
                    d = _fields(r)
                    out.append(make_job(config, "college_interfolio", title,
                                        f"https://apply.interfolio.com/{pid}", d["location"],
                                        d["description"], d["posted"], d["department"]))
                    if len(out) >= MAX_JOBS:
                        return out
                time.sleep(PAUSE)
        for purl in config.get("position_urls") or []:
            m = re.search(r"apply\.interfolio\.com/(\d+)", purl)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            time.sleep(PAUSE)
            d = _position(m.group(1), tenant)
            if d.get("title") and title_ok(d["title"]):
                out.append(make_job(config, "college_interfolio", d["title"], purl, d.get("location", ""),
                                    d.get("description", ""), d.get("posted", ""), d.get("department", "")))
        return out
    except Exception:
        return []
