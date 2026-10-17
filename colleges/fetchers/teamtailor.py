"""Teamtailor career sites (<slug>.teamtailor.com or a custom host such as
jobs.ccri.edu that loads teamtailor-cdn.com assets).

config: {"url": "https://<host>/jobs", "host": "<host>"}
GET https://<host>/jobs.rss lists every opening with the full description,
pubDate, department and locations in the item, so no detail fetch is needed.
Verified against Community College of Rhode Island (jobs.ccri.edu).
"""
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "teamtailor"
needs_browser = False
TT_NS = "{https://teamtailor.com/locations}"


def detect(url, html=""):
    host = urlsplit(url or "").netloc.lower()
    if host.endswith(".teamtailor.com") and host.split(".")[0] not in ("www", "app", "api", "cdn"):
        return {"url": f"https://{host}/jobs", "host": host}
    m = re.search(r"https?://([a-z0-9-]+\.teamtailor\.com)", html or "", re.I)
    if m and m.group(1).split(".")[0] not in ("www", "app", "api", "cdn"):
        return {"url": f"https://{m.group(1).lower()}/jobs", "host": m.group(1).lower()}
    if host and "teamtailor-cdn.com" in (html or ""):
        return {"url": f"https://{host}/jobs", "host": host}
    return None


def _date(s):
    try:
        return parsedate_to_datetime(clean(s)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def fetch(config):
    try:
        host = config.get("host") or urlsplit(config["url"]).netloc
        r = requests.get(f"https://{host}/jobs.rss", headers=dict(HEADERS, Accept="*/*"), timeout=25)
        if r.status_code != 200 or b"<rss" not in r.content[:500]:
            return []
        root = ET.fromstring(r.content)
        out, seen = [], set()
        for it in root.findall(".//item"):
            title = clean(it.findtext("title") or "")
            link = clean(it.findtext("link") or "")
            if not title or not link or link in seen or not title_ok(title):
                continue
            seen.add(link)
            locs = []
            for L in it.findall(f"{TT_NS}locations/{TT_NS}location"):
                s = clean(L.findtext(f"{TT_NS}city") or L.findtext(f"{TT_NS}name") or L.text or "")
                if s and s not in locs:
                    locs.append(s)
            loc = "; ".join(locs)
            out.append(make_job(config, "college_teamtailor", title, link, location=loc,
                                description=strip_html(it.findtext("description") or "")[:20000],
                                posted=_date(it.findtext("pubDate") or ""),
                                department=clean(it.findtext(f"{TT_NS}department") or "")))
        return out
    except Exception:
        return []
