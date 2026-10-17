"""HigherEdJobs aggregator: https://www.higheredjobs.com

The HTML site (search.cfm, details.cfm, even the home page) sits behind an
Incapsula bot wall that returns "Request unsuccessful" to requests AND to
Playwright (headless, headless=new, headed Chrome with a persistent profile all
blocked at the time of writing). The per-category RSS feeds are *not* behind the
wall, so this fetcher is RSS-only:

    https://www.higheredjobs.com/rss/categoryFeed.cfm?catID=<id>

Each <item> carries title, "<Institution> (<City>, <ST>)" as the description,
the details.cfm link and a pubDate. Detail pages are unreachable, so
description text is synthesised from the feed fields (no pay / AI hits).

Category ids (from /rss/): 102 Computer Science, 116 Computer Engineering,
290 Data Science, 144 Information Systems and Technology. Cybersecurity has no
category of its own; such postings land in 102/144. Note that JobCat=104 on the
search page is Mathematics, not CS.
"""
import re
import time
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests

from ._common import HEADERS, make_job, title_ok, clean

name = "higheredjobs"
needs_browser = False
BASE = "https://www.higheredjobs.com"
CATEGORIES = {
    102: "Computer Science",
    116: "Computer Engineering",
    290: "Data Science",
    144: "Information Systems and Technology",
}
# Feed is category-wide; PosType is accepted but we filter on title anyway.
FEED = BASE + "/rss/categoryFeed.cfm"
_LOC_RE = re.compile(r"^(?P<inst>.*?)\s*\((?P<loc>[^()]*)\)\s*$")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _date(s):
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return ""


def _split_inst(desc):
    """'University of X (City, ST)' -> ('University of X', 'City, ST')."""
    desc = clean(desc)
    m = _LOC_RE.match(desc)
    if m:
        return clean(m.group("inst")), clean(m.group("loc"))
    return desc, ""


def _feed(session, cat_id):
    try:
        r = session.get(FEED, params={"catID": cat_id}, timeout=30)
        if r.status_code != 200 or b"<rss" not in r.content[:300]:
            return []
        root = ET.fromstring(r.content)
    except Exception:
        return []
    return root.findall("./channel/item")


def fetch(config):
    out, seen = [], set()
    session = requests.Session()
    session.headers.update(HEADERS)
    for cat_id, cat_name in CATEGORIES.items():
        items = _feed(session, cat_id)
        for it in items:
            title = clean(it.findtext("title", default=""))
            url = clean(it.findtext("link", default="") or it.findtext("guid", default=""))
            if not url or url in seen or not title_ok(title):
                continue
            seen.add(url)
            inst, loc = _split_inst(it.findtext("description", default=""))
            posted = _date(it.findtext("pubDate", default=""))
            desc = (f"{title} at {inst}" + (f" ({loc})" if loc else "")
                    + f". Listed in the HigherEdJobs \"{cat_name}\" category. "
                    "Full posting text is behind HigherEdJobs' bot wall; open the link for details.")
            try:
                job = make_job(config, "college_higheredjobs", title, url, loc, desc, posted, cat_name)
            except Exception:
                continue
            if inst:
                job.company = inst
            out.append(job)
        time.sleep(1)  # be polite between category feeds
    return out
