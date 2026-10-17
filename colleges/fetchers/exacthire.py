"""ExactHire career sites: https://<tenant>.exacthire.com/

config: {"url": "https://<tenant>.exacthire.com/", "site_id": "286"}
The page is a JS app, but it inlines {"id": <site_id>, "name": ..., "subdomain":
"<tenant>"} and the public API
GET https://api.exacthire.com/api/public/career_sites/<site_id>/positions?page=N
returns content.positions[] (each wrapped as {"0": {...}, "distance": ...}) with
id, title, location.name, employment_type_id, created_at, decoded_description.
Verified against Eckerd College (site 286, 14 positions).
"""
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "exacthire"
needs_browser = False
HOST_RE = re.compile(r"https?://([a-z0-9-]+)\.exacthire\.com", re.I)
SITE_RE = re.compile(r'"id":(\d+),"name":"[^"]*","subdomain":"([a-z0-9-]+)"', re.I)
API = "https://api.exacthire.com/api/public/career_sites/{}/positions?page={}"


def detect(url, html=""):
    m = HOST_RE.search(url or "")
    if not m or m.group(1).lower() in ("www", "api", "assets", "app"):
        return None
    tenant = m.group(1).lower()
    site = ""
    for sid, sub in SITE_RE.findall(html or ""):
        if sub.lower() == tenant:
            site = sid
            break
    if not site:
        try:
            r = requests.get(f"https://{tenant}.exacthire.com/", headers=HEADERS, timeout=20)
            for sid, sub in SITE_RE.findall(r.text):
                if sub.lower() == tenant:
                    site = sid
                    break
        except requests.RequestException:
            pass
    if not site:
        return None
    return {"url": f"https://{tenant}.exacthire.com/", "site_id": site}


def fetch(config):
    site = config.get("site_id")
    if not site:
        return []
    base = config["url"].rstrip("/")
    out, seen = [], set()
    for page in range(1, 20):
        try:
            r = requests.get(API.format(site, page), headers=dict(HEADERS, Accept="application/json"), timeout=30)
            if r.status_code != 200:
                break
            items = ((r.json() or {}).get("content") or {}).get("positions") or []
        except (requests.RequestException, ValueError):
            break
        new = 0
        for w in items:
            p = w.get("0") if isinstance(w, dict) and "0" in w else w
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "")
            title = clean(p.get("title") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            new += 1
            if not title_ok(title):
                continue
            loc = clean((p.get("location") or {}).get("name") or "")
            out.append(make_job(config, "college_exacthire", title, f"{base}/?position={pid}", loc,
                                strip_html(p.get("decoded_description") or ""), (p.get("created_at") or "")[:10]))
        if not new or len(items) < 10:
            break
    return out
