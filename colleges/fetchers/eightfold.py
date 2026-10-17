"""Eightfold career hubs on a customer host, e.g. https://hiring.jhu.edu/careers

config: {"url": "https://<host>/careers", "host": "https://<host>", "domain": "<domain>"}
The page embeds window._EF_GROUP_ID = "<domain>" and "pcsx" config. Search is
  GET <host>/api/pcsx/search?domain=<domain>&query=&start=N&num=10
  -> data.count, data.positions[{id, name, locations, postedTs, creationTs,
     department, workLocationOption, positionUrl}]   (num is capped at 10)
and the body comes from GET <host>/api/apply/v2/jobs/<id>?domain=<domain>
(job_description, t_update, t_create); only title matches are fetched.
Verified against Johns Hopkins (hiring.jhu.edu, ~600 postings).
"""
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "eightfold"
needs_browser = False
JSON_HEADERS = dict(HEADERS, Accept="application/json")
PAGE = 10
MAX_JOBS = 2000


def detect(url, html=""):
    p = urlsplit(url or "")
    html = html or ""
    ef_host = p.netloc.lower().endswith(".eightfold.ai")
    if not ef_host and not (re.search(r"window\._EF_GROUP_ID\s*=|pcsxConfig|/api/pcsx/", html)
                            and re.search(r"eightfold", html, re.I)):
        return None
    domain = (parse_qs(p.query).get("domain") or [""])[0]
    if not domain:
        m = re.search(r'window\._EF_GROUP_ID\s*=\s*"([^"]+)"', html)
        domain = m.group(1) if m and m.group(1) != "Unknown" else ""
    if not domain:
        m = re.search(r'domain(?:&#34;|"):\s*(?:&#34;|")([a-z0-9.-]+\.[a-z]+)', html, re.I)
        domain = m.group(1) if m else ""
    if not domain or p.netloc.lower() in ("eightfold.ai", "www.eightfold.ai"):
        return None
    host = f"{p.scheme or 'https'}://{p.netloc}"
    return {"url": f"{host}/careers?domain={domain}" if ef_host else f"{host}/careers",
            "host": host, "domain": domain}


def _ts(v):
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime("%Y-%m-%d") if v else ""
    except Exception:
        return ""


def _detail(host, pid, domain):
    try:
        r = requests.get(f"{host}/api/apply/v2/jobs/{pid}", params={"domain": domain},
                         headers=JSON_HEADERS, timeout=25)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def fetch(config):
    try:
        host = (config.get("host") or "").rstrip("/")
        domain = config.get("domain") or ""
        if not host:
            p = urlsplit(config["url"])
            host = f"{p.scheme}://{p.netloc}"
        out, seen, start = [], set(), 0
        while start < MAX_JOBS:
            r = requests.get(f"{host}/api/pcsx/search", headers=JSON_HEADERS, timeout=30,
                             params={"domain": domain, "query": "", "start": start, "num": PAGE})
            if r.status_code != 200:
                break
            data = (r.json() or {}).get("data") or {}
            rows = data.get("positions") or []
            if not rows:
                break
            for j in rows:
                pid = str(j.get("id") or "")
                title = clean(j.get("name") or "")
                if not pid or pid in seen or not title_ok(title):
                    continue
                seen.add(pid)
                href = f"{host}{j.get('positionUrl') or '/careers/job/' + pid}"
                if domain and host.endswith(".eightfold.ai"):
                    href += f"?domain={domain}"
                loc = clean(", ".join(j.get("locations") or []))
                if (j.get("workLocationOption") or "").startswith("remote") and "remote" not in loc.lower():
                    loc = f"{loc} (Remote)".strip()
                d = _detail(host, pid, domain)
                out.append(make_job(config, "college_eightfold", title, href, location=loc,
                                    description=strip_html(d.get("job_description") or "")[:20000],
                                    posted=_ts(j.get("postedTs") or d.get("t_create") or j.get("creationTs")),
                                    department=clean(j.get("department") or d.get("department") or "")))
            start += len(rows)
            if start >= int(data.get("count") or 0):
                break
            time.sleep(0.2)
        return out
    except Exception:
        return []
