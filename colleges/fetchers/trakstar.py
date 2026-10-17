"""Trakstar Hire (ex RecruiterBox): https://<slug>.hire.trakstar.com/
(also <slug>.recruiterbox.com and the public JSON
https://jsapi.recruiterbox.com/v1/openings?client_name=<slug> that schools embed).

config: {"url": "https://<slug>.hire.trakstar.com/", "trakstar_slug": "<slug>", "client_name": "<slug>"}
The jsapi JSON (objects[{id, title, description HTML, location{city,state},
hosted_url, allows_remote, position_type, team, close_date}], offset/limit
paging, limit up to 100) is preferred when client_name is known (it is the
host slug by default); otherwise the hosted page's RSS feed
/jobfeeds/<feed> is read (items: title, link /jobs/<id>, description with the
full body, job:location, job:position, pubDate). /api/v2/openings/ needs an
API key (401). Verified against Bennington College (RSS, 11 jobs) and Hudson
Valley Community College (hvcc JSON, 27 jobs).
"""
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "trakstar"
needs_browser = False
SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.(?:hire\.trakstar\.com|recruiterbox\.com)", re.I)
JSAPI_RE = re.compile(r"https?://jsapi\.recruiterbox\.com/v1/openings\?[^\s\"'<>]*client_name=([a-z0-9-]+)", re.I)
JSAPI = "https://jsapi.recruiterbox.com/v1/openings"
FEED_RE = re.compile(r"""href=["']([^"']*/jobfeeds/[a-z0-9-]+)["']""", re.I)
JOB_NS = "{https://recruiterbox.com/rss/job/}"


def detect(url, html=""):
    h = (html or "").replace("&amp;", "&")
    j = JSAPI_RE.search(url or "") or JSAPI_RE.search(h)
    m = SLUG_RE.search(url or "") or SLUG_RE.search(h)
    if m and m.group(1).lower() in ("www", "app", "api", "static", "help", "support", "jsapi"):
        m = None
    if not m and not j:
        return None
    slug = (m.group(1) if m else j.group(1)).lower()
    return {"url": f"https://{slug}.hire.trakstar.com/", "trakstar_slug": slug,
            "client_name": j.group(1).lower() if j else slug}


def _fetch_json(config, client):
    """Public jsapi feed; [] when the client name is unknown to it."""
    out, seen, offset = [], set(), 0
    while offset < 2000:
        r = requests.get(JSAPI, params={"client_name": client, "offset": offset, "limit": 100},
                         headers=dict(HEADERS, Accept="application/json"), timeout=30)
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            break
        d = r.json() or {}
        rows = d.get("objects") or []
        for j in rows:
            jid = str(j.get("id") or "")
            title = clean(j.get("title") or "")
            if not jid or jid in seen or not title_ok(title):
                continue
            seen.add(jid)
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in (clean(loc.get("city") or ""), clean(loc.get("state") or "")) if x) if isinstance(loc, dict) else clean(str(loc))
            if j.get("allows_remote") and "remote" not in loc_s.lower():
                loc_s = f"{loc_s} (Remote)".strip()
            body = j.get("description") or ""
            if j.get("position_type"):
                body = f"Position type: {j['position_type']}\n{body}"
            out.append(make_job(config, "college_trakstar", title, j.get("hosted_url") or f"https://{client}.hire.trakstar.com/jobs/{jid}/",
                                location=loc_s, description=strip_html(body)[:20000],
                                department=clean(j.get("team") or "")))
        offset += len(rows)
        if not rows or offset >= int((d.get("meta") or {}).get("total") or 0):
            break
    return out


def _feed_url(base, slug):
    try:
        r = requests.get(base, headers=HEADERS, timeout=25)
        m = FEED_RE.search(r.text) if r.status_code == 200 else None
        if m:
            return urljoin(base, m.group(1))
    except Exception:
        pass
    return urljoin(base, f"/jobfeeds/{slug}")


def _date(s):
    try:
        return parsedate_to_datetime(clean(s)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def fetch(config):
    try:
        slug = config.get("trakstar_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        try:
            jobs = _fetch_json(config, config.get("client_name") or slug)
            if jobs:
                return jobs
        except Exception:
            pass
        base = f"https://{slug}.hire.trakstar.com/"
        r = requests.get(_feed_url(base, slug), headers=dict(HEADERS, Accept="*/*"), timeout=30)
        if r.status_code != 200 or b"<rss" not in r.content[:1000]:
            return []
        root = ET.fromstring(r.content.strip())
        out, seen = [], set()
        for it in root.findall(".//item"):
            title = clean(it.findtext("title") or "")
            link = clean(it.findtext("link") or "")
            link = re.sub(r"^http://", "https://", link)
            if not title or not link or link in seen or not title_ok(title):
                continue
            seen.add(link)
            body = it.findtext("description") or ""
            loc = clean(it.findtext(f"{JOB_NS}location") or "")
            if not loc:
                m = re.search(r"Location:\s*([^<]+)", body)
                loc = clean(m.group(1)) if m else ""
            out.append(make_job(config, "college_trakstar", title, link, location=loc.replace(",", ", "),
                                description=strip_html(body)[:20000],
                                posted=_date(it.findtext("pubDate") or ""),
                                department=clean(it.findtext(f"{JOB_NS}team") or "")))
        return out
    except Exception:
        return []
