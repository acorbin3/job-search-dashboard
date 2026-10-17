"""Beamery Grow career sites on a school host (Umbraco front end), e.g. SNHU:
  https://jobs.snhu.edu/en/jobs/

config: {"url": "https://<host>/en/jobs/"}
Server-rendered to plain requests with a browser UA: the list page has
div.card-body > h2.card-title > a.js-view-job[href="/en/jobs/<reqid>/<slug>/"]
with ul.job-meta (team, location) and pages at ?page=N (stop at a page with
no new links). The detail page holds the body in div.job-detail /
article.cms-content and the type / posted date / location in the header.
Detected by the beamery.com talent-community link plus the js-view-job markup.
Verified against Southern New Hampshire University.
"""
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "beamery"
needs_browser = False
MAX_PAGES = 60
LINK_RE = re.compile(r"/jobs/[A-Za-z0-9-]+/[^/]+/?$", re.I)


def detect(url, html=""):
    h = html or ""
    p = urlsplit(url or "")
    if not p.netloc or "beamery.com" not in h or "js-view-job" not in h:
        return None
    m = re.search(r"/(?:[a-z]{2}/)?jobs/", p.path)
    return {"url": f"https://{p.netloc.lower()}{m.group(0) if m else '/en/jobs/'}"}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        box = soup.find("div", class_="job-detail") or soup.find("article", class_="cms-content") or soup.find("main")
        text = strip_html(str(box)) if box else ""
        m = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", text[:600])
        posted = ""
        if m:
            try:
                posted = datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
            except Exception:
                pass
        return text[:20000], posted
    except Exception:
        return "", ""


def fetch(config):
    try:
        base = config["url"]
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            r = requests.get(base, params={"page": page}, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            new = 0
            for a in soup.select("a.js-view-job[href], h2.card-title a[href]"):
                href = urljoin(base, a["href"]).split("?")[0]
                title = clean(a.get_text(" ", strip=True))
                if not LINK_RE.search(urlsplit(href).path) or href in seen:
                    continue
                seen.add(href)
                new += 1
                if not title_ok(title):
                    continue
                card = a.find_parent("div", class_="card-body") or a.parent
                meta = [clean(li.get_text(" ", strip=True)) for li in card.select("ul.job-meta li")] if card else []
                team = card.select_one("li.job-team") if card else None
                dept = clean(team.get_text(" ", strip=True)) if team else ""
                loc = next((m for m in meta if m and m != dept), "")
                body, posted = _detail(href)
                out.append(make_job(config, "college_beamery", title, href, location=loc, description=body,
                                    posted=posted, department=dept))
            if not new:
                break
        return out
    except Exception:
        return []
