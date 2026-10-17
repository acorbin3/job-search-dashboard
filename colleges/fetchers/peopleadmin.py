"""PeopleAdmin (PowerSchool) boards: https://jobs.<school>.edu/postings/search

Every PeopleAdmin 7 site serves an Atom feed of search results at
/postings/search.atom?query=<kw>. Detail pages carry the description in a
table of labelled rows; we grab the whole main column as text.
"""
import re
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "peopleadmin"
needs_browser = False
QUERIES = ["adjunct", "instructor", "lecturer", "faculty", "professor"]
NS = {"a": "http://www.w3.org/2005/Atom"}


def _is_pa(base):
    try:
        r = requests.get(f"{base}/postings/search.atom", params={"query": "a"}, headers=HEADERS, timeout=15)
        return r.status_code == 200 and b"<feed" in r.content[:500]
    except Exception:
        return False


def detect(url, html=""):
    h = html or ""
    hosts = []
    p = urlsplit(url)
    if "/postings/search" in url or "peopleadmin" in p.netloc.lower():
        hosts.append(f"{p.scheme}://{p.netloc}")
    for m in re.finditer(r"https?://[a-z0-9.-]+(?=/postings(?:/search|/\d+|\b))", h, re.I):
        hosts.append(m.group(0))
    if ('href="/postings/search' in h or "PeopleAdmin" in h) and f"{p.scheme}://{p.netloc}" not in hosts:
        hosts.append(f"{p.scheme}://{p.netloc}")
    seen = set()
    for base in hosts:
        base = base.rstrip("/")
        if base in seen:
            continue
        seen.add(base)
        if _is_pa(base):
            return {"url": base}
    return None


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        main = soup.find(id="main-content") or soup.find("main") or soup.body
        loc = ""
        for th in soup.find_all("th"):
            t = th.get_text(" ", strip=True).lower()
            if t in ("location", "campus", "work location", "city", "job location"):
                td = th.find_next("td")
                if td:
                    loc = clean(td.get_text(" ", strip=True))
                    break
        return strip_html(str(main)) if main else "", loc
    except Exception:
        return "", ""


def fetch(config):
    base = config["url"].rstrip("/")
    out, seen = [], set()
    for q in QUERIES:
        try:
            r = requests.get(f"{base}/postings/search.atom", params={"query": q},
                             headers=HEADERS, timeout=25)
            if r.status_code != 200 or b"<feed" not in r.content[:500]:
                continue
            root = ET.fromstring(r.content)
        except Exception:
            continue
        for e in root.findall("a:entry", NS):
            title = clean((e.findtext("a:title", default="", namespaces=NS)))
            link = e.find("a:link", NS)
            url = link.get("href") if link is not None else ""
            if not url or url in seen or not title_ok(title):
                continue
            seen.add(url)
            posted = (e.findtext("a:published", default="", namespaces=NS)
                      or e.findtext("a:updated", default="", namespaces=NS))[:10]
            desc, loc = _detail(url)
            out.append(make_job(config, "college_peopleadmin", title, url, loc, desc, posted))
    return out
