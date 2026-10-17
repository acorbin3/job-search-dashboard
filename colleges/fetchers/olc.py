"""Online Learning Consortium "Industry Job Postings":
https://onlinelearningconsortium.org/about/industry-job-postings/

Not a job board: a hand-curated WordPress/Elementor page listing a handful of
member openings, each as a small block (title, institution, "Learn More &
Apply" button linking to the school's own ATS). We read the blocks, keep the
ones whose title passes title_ok, and link straight to the external posting.
No description is fetched (the targets are arbitrary ATSes).
"""
import re

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, clean

name = "olc"
needs_browser = False
PAGE = "https://onlinelearningconsortium.org/about/industry-job-postings/"
BUTTON_RE = re.compile(r"learn more|apply|view (job|posting)|details", re.I)


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _blocks(html):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href^='http']"):
        if not BUTTON_RE.search(a.get_text(" ", strip=True) or ""):
            continue
        if "onlinelearningconsortium.org" in a["href"]:
            continue
        node, texts = a, []
        for _ in range(8):           # climb to the smallest ancestor that also holds the title text
            node = node.parent
            if node is None:
                break
            texts = [clean(t) for t in node.stripped_strings if clean(t)]
            if len(texts) >= 2 and not BUTTON_RE.search(texts[0]):
                break
        if len(texts) < 2 or len(" ".join(texts)) > 600:
            continue
        yield {"title": texts[0], "institution": texts[1] if not BUTTON_RE.search(texts[1]) else "", "url": a["href"]}


def fetch(config):
    out, seen = [], set()
    try:
        r = requests.get(PAGE, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        for b in _blocks(r.text):
            if b["url"] in seen or not title_ok(b["title"]):
                continue
            seen.add(b["url"])
            cfg = dict(config)
            if b["institution"]:
                cfg["name"] = b["institution"]
            out.append(make_job(cfg, "college_olc", b["title"], b["url"], "", "", ""))
    except Exception:
        return out
    return out
