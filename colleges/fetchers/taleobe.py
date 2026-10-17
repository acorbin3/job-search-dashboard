"""Taleo Business Edition career sites (tbe.taleo.net), not the Taleo
Enterprise careersection flavour that taleo.py handles:
  https://<pod>.tbe.taleo.net/<pod>NN/ats/careers/v2/searchResults?org=<ORG>&cws=<n>

config: {"url": <searchResults url>, "org": "ADELPHI", "cws": "43"}
The results page is server-rendered, 10 per page; each opening is an
accordion head with a.viewJobLink plus department / type / location divs.
Paging is the "next" link (rowFrom=10...) which only works inside the same
session. viewRequisition?rid=<id> carries the posting body.
Verified against Adelphi University (also Campbell University).
"""
import re
from urllib.parse import urljoin, urlsplit, parse_qs

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "taleobe"
needs_browser = False
MAX_PAGES = 40

URL_RE = re.compile(r"https?://[a-z0-9-]+\.tbe\.taleo\.net/[a-z0-9]+/ats/careers/(?:v2/)?[A-Za-z]+\?[^\"'\s<>]*org=[A-Za-z0-9_-]+[^\"'\s<>]*", re.I)


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search((html or "").replace("&amp;", "&"))
    if not m:
        return None
    u = m.group(0).replace("&amp;", "&")
    q = parse_qs(urlsplit(u).query)
    org, cws = (q.get("org") or [""])[0], (q.get("cws") or ["1"])[0]
    base = u.split("/ats/careers/")[0]
    return {"url": f"{base}/ats/careers/v2/searchResults?org={org}&cws={cws}", "org": org, "cws": cws}


def _detail(sess, url):
    try:
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "form"]):
            t.decompose()
        box = soup.find(id="oracletaleocwsv2-wrapper") or soup.body
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        url = config["url"]
        out, seen = [], set()
        for _ in range(MAX_PAGES):
            r = sess.get(url, timeout=25)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            heads = soup.select(".oracletaleocwsv2-accordion-head-info")
            for h in heads:
                a = h.find("a", class_="viewJobLink") or h.find("a", href=re.compile("viewRequisition"))
                if not a:
                    continue
                title = clean(a.get_text(" ", strip=True))
                href = urljoin(url, a["href"])
                m = re.search(r"rid=(\d+)", href)
                key = m.group(1) if m else href
                if not title or key in seen or not title_ok(title):
                    continue
                seen.add(key)
                cols = [clean(d.get_text(" ", strip=True)) for d in h.find_all("div", recursive=False)]
                dept = cols[0] if cols else ""
                loc = cols[-1] if len(cols) > 1 else ""
                out.append(make_job(config, "college_taleobe", title, href, location=loc,
                                    description=_detail(sess, href), department=dept))
            nxt = soup.find("a", href=re.compile(r"searchResults\?next"))
            if not nxt or not heads:
                break
            url = urljoin(url, nxt["href"].replace("&amp;", "&"))
        return out
    except Exception:
        return []
