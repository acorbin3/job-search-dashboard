"""Fallback for job boards whose ATS we don't recognise.

Fetches the discovered listing page (config["url"]), collects anchors that
look like postings (link text passes title_ok and the href looks like a
posting), and returns them with a best-effort description from the detail
page. Low precision per board, but it keeps the long tail of small colleges in
the scan. Optional config["pages"]: extra listing URLs to read.
Optional config["browser"]: true -> load the listing pages in headless
Chromium (colleges.discover.get_browser) for JS-rendered or bot-walled boards.
Optional config["pdf_links"]: true -> PDF/DOC links count as postings (some
small colleges post each vacancy as a PDF), with the link text as title.
"""
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean, scrape

name = "generic"
needs_browser = False

SKIP_HREF = re.compile(r"\.(pdf|docx?|xlsx?|jpg|png|gif|mp4)(\?|$)|mailto:|tel:|javascript:|#$", re.I)
NAV_TEXT = re.compile(r"^(home|about|contact|login|log in|apply now|search|menu|skip to|faculty & staff|faculty and staff|faculty/staff|faculty resources|faculty & staff resources|faculty directory|faculty staff directory|student|alumni|give|donate|news|events|calendar|library|athletics|admissions|academics|programs|apply|visit|request info|employment|careers|jobs|job opportunities|human resources|hr)$", re.I)


MORE_TEXT = re.compile(r"^(more info(rmation)?|details|view( details| job| posting| position)?|apply( now| here)?|read more|learn more|download( document)?|job description|click here|full description|open|link)\W*$", re.I)


def _title_near(a):
    """Title for a card/\"More info\" link: a heading inside the anchor, else the
    nearest heading/strong before it inside the same card."""
    h = a.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
    if h and clean(h.get_text(" ", strip=True)):
        return clean(h.get_text(" ", strip=True))
    node = a
    for _ in range(4):                              # walk up a few container levels
        node = node.parent
        if node is None or node.name in ("body", "html", "main", "ul", "table", "tbody"):
            break
        h = node.find(["h1", "h2", "h3", "h4", "h5", "strong", "b", "dt"])
        if h and h is not a and clean(h.get_text(" ", strip=True)):
            return clean(h.get_text(" ", strip=True))
    prev = a.find_previous(["h1", "h2", "h3", "h4", "h5", "dt"])
    return clean(prev.get_text(" ", strip=True)) if prev else ""


def detect(url, html=""):
    return None


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer"]):
            t.decompose()
        main = soup.find("main") or soup.find(id=re.compile("content|main", re.I)) or soup.body
        return strip_html(str(main))[:20000] if main else ""
    except Exception:
        return ""


DOC_HREF = re.compile(r"\.(pdf|docx?)(\?|$)", re.I)
GENERIC_NAV = re.compile(r"\b(positions?|openings?|opportunit\w*|employment|careers?|jobs?|listings?|"
                         r"applications?|apply|handbook|resources|directory|information|benefits|hiring|"
                         r"recruit\w*|portal|login|search|page|home|vacanc\w*)\b", re.I)


def _listing(lu, browser):
    """(final_url, html) for a listing page, via requests or headless Chromium."""
    if browser:
        from colleges.discover import get_browser
        code, final, html = get_browser(lu)
        if code != 200:                 # SiteGround-style walls pass on the 2nd load
            code, final, html = get_browser(lu)
        return (final, html) if code == 200 and html else (None, None)
    r = requests.get(lu, headers=HEADERS, timeout=25)
    return (r.url, r.text) if r.status_code == 200 else (None, None)


def fetch(config):
    urls = [config["url"]] + list(config.get("pages") or [])
    browser, pdf_links = bool(config.get("browser")), bool(config.get("pdf_links"))
    out, seen = [], set()
    for lu in urls[:4]:
        try:
            final, html = _listing(lu, browser)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            continue
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text(" ", strip=True))
            href = a["href"].strip()
            is_doc = bool(DOC_HREF.search(href))
            if len(text) > 160 or MORE_TEXT.match(text or ""):
                text = _title_near(a) or text
            if not text or len(text) < 8 or len(text) > 160 or NAV_TEXT.match(text):
                continue
            if SKIP_HREF.search(href) and not (pdf_links and is_doc):
                continue
            text = re.sub(r"\s*(\(pdf\)|\[pdf\]|\.pdf)$", "", text, flags=re.I)
            if not title_ok(text):
                continue
            if not scrape.title_matches(text) and GENERIC_NAV.search(text):
                continue   # "Adjunct Faculty Openings" style navigation links
            url = urljoin(final, href)
            if url in seen or urlsplit(url).scheme not in ("http", "https"):
                continue
            seen.add(url)
            desc = "" if is_doc else _detail(url)
            out.append(make_job(config, "college_generic", text, url, "", desc))
            if len(out) >= 60:
                break
    return out
