"""BrightMove job portals: http://j.brt.mv/ax.do?portalGK=<n> (also <slug>.brightmove.com)

config: {"url": "<portal url>", "company_gk": "<n>", "portal_gk": "<n>"}
The portal page links its RSS feed (JobRSS.do?companyGK=&portalGK=), whose
items carry the title, an ejob.bz link and the full description inline. When
the ids are missing the portal table (tr.BMData: PortalViewRequirement.do
link + location cell) is parsed and the detail page read instead.
Verified against University of the Potomac.
"""
import re
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "brightmove"
needs_browser = False

HOST_RE = re.compile(r"https?://(?:j\.brt\.mv|ejob\.bz|[a-z0-9-]+\.brightmove\.com)/[^\"'\s<>]*", re.I)
GK_RE = re.compile(r"(company|portal)GK=(\d+)", re.I)


def _gks(text):
    return {k.lower(): v for k, v in GK_RE.findall((text or "").replace("&amp;", "&"))}


def detect(url, html=""):
    m = HOST_RE.search(url or "") or HOST_RE.search(html or "")
    if not m:
        return None
    gk = _gks(html)
    gk.update(_gks(url))
    if not gk.get("portal"):
        return None
    cfg = {"url": m.group(0).replace("&amp;", "&"), "portal_gk": gk["portal"]}
    if gk.get("company"):
        cfg["company_gk"] = gk["company"]
    return cfg


def _rss(host, company, portal, config):
    r = requests.get(f"https://{host}/JobRSS.do?companyGK={company}&portalGK={portal}", headers=HEADERS, timeout=25)
    if r.status_code != 200 or "<rss" not in r.text[:500]:
        return None
    out, seen = [], set()
    for it in ET.fromstring(r.content).iter("item"):
        title = clean(it.findtext("title") or "")
        href = (it.findtext("link") or "").strip()
        loc = ""
        if " - " in title:          # "Title - City, ST"
            title, loc = (x.strip() for x in title.rsplit(" - ", 1))
        if not title or href in seen or not title_ok(title):
            continue
        seen.add(href)
        out.append(make_job(config, "college_brightmove", title, href, location=loc,
                            description=strip_html(it.findtext("description") or "")[:20000]))
    return out


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        box = BeautifulSoup(r.text, "html.parser").find(id="portal_container")
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        url = config["url"]
        host = urlsplit(url).netloc
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        gk = _gks(r.text)
        company = config.get("company_gk") or gk.get("company")
        portal = config.get("portal_gk") or gk.get("portal")
        if company and portal:
            jobs = _rss(host, company, portal, config)
            if jobs is not None:
                return jobs
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"PortalViewRequirement\.do\?reqGK=\d+", re.I)):
            title = clean(a.get_text(" ", strip=True))
            href = urljoin(url, a["href"])
            if not title or href in seen or not title_ok(title):
                continue
            seen.add(href)
            tds = a.find_parent("tr").find_all("td") if a.find_parent("tr") else []
            loc = clean(tds[1].get_text(" ", strip=True)) if len(tds) > 1 else ""
            out.append(make_job(config, "college_brightmove", title, href, location=loc, description=_detail(href)))
        return out
    except Exception:
        return []
