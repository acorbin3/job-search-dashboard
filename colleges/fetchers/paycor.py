"""Paycor Recruiting (recruitingbypaycor.com, ex-Newton) career pages.

config: {"url": "https://recruitingbypaycor.com/career/CareerHome.action?clientId=<id>", "client_id": "<id>"}
CareerHome.action lists every opening grouped by department; each row is a
JobIntroduction.action link with the location in the sibling div.
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "paycor"
needs_browser = False

CLIENT_RE = re.compile(r"recruitingbypaycor\.com/career/[A-Za-z]+\.action\?[^\"'\s<>]*clientId=([0-9a-f]{20,})", re.I)


def detect(url, html=""):
    m = CLIENT_RE.search(url or "") or CLIENT_RE.search((html or "").replace("&amp;", "&"))
    if not m:
        return None
    cid = m.group(1)
    return {"url": f"https://recruitingbypaycor.com/career/CareerHome.action?clientId={cid}", "client_id": cid}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        box = soup.find(class_=re.compile("gnewtonJobDescription|jobDescription|job-description", re.I)) or soup.body
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    cid = config.get("client_id") or (CLIENT_RE.search(config["url"]) or [None, ""])[1]
    url = f"https://recruitingbypaycor.com/career/CareerHome.action?clientId={cid}"
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    dept = ""
    for el in soup.find_all(class_=re.compile("gnewtonCareerGroup(Header|Row)Class")):
        if "gnewtonCareerGroupHeaderClass" in el.get("class", []):
            dept = clean(el.get_text(" ", strip=True))
            continue
        a = el.find("a", href=re.compile("JobIntroduction", re.I))
        if not a:
            continue
        title = clean(a.get_text(" ", strip=True))
        href = urljoin(url, a["href"])
        if not title or href in seen or not title_ok(title):
            continue
        seen.add(href)
        loc_el = el.find(class_=re.compile("JobDescriptionClass", re.I))
        loc = clean(loc_el.get_text(" ", strip=True)) if loc_el else ""
        out.append(make_job(config, "college_paycor", title, href, location=loc,
                            description=_detail(href), department=dept))
    return out
