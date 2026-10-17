"""Plain RSS/Atom job feeds on a school's own site.

config: {"ats": "college_rss", "url": "<feed url>"}; optional "pages": more feeds.
Covers Drupal/WordPress boards that expose a feed but render listings with
"Read more" links (e.g. https://careers.umich.edu/search/feed/advanced?...,
https://www.gwinnettcollege.edu/feed/?post_type=careers). Not auto-detected:
record the feed URL by hand.
"""
import re

import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "rss"
needs_browser = False


def detect(url, html=""):
    return None


def fetch(config):
    out, seen = [], set()
    for fu in [config["url"]] + list(config.get("pages") or []):
        try:
            r = requests.get(fu, headers=dict(HEADERS, Accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"), timeout=25)
            if r.status_code != 200:
                continue
        except requests.RequestException:
            continue
        if "<rss" not in r.text[:3000] and "<feed" not in r.text[:3000]:
            continue
        soup = BeautifulSoup(r.text, "html.parser")   # no lxml: html.parser copes with RSS
        for it in soup.find_all(["item", "entry"]):
            title = clean((it.find("title").get_text() if it.find("title") else ""))
            link_el = it.find("link")
            # html.parser treats <link> as void, so RSS link text lands after the tag
            link = clean(link_el.get("href") or (link_el.next_sibling if isinstance(link_el.next_sibling, str) else "") or link_el.get_text()) if link_el else ""
            if not link:
                g = it.find("guid")
                link = clean(g.get_text()) if g else ""
            if not title or not link or link in seen or not title_ok(title):
                continue
            seen.add(link)
            desc = it.find(["description", "content", "summary", "content:encoded"])
            posted = it.find(["pubDate", "published", "updated", "dc:date"])
            out.append(make_job(config, "college_rss", title, link, "", strip_html(desc.get_text()) if desc else "",
                                (posted.get_text().strip() if posted else "")[:25]))
    return out
