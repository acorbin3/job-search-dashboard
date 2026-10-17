"""HBCU Careers: https://www.hbcucareers.com (JobBoardHQ / ASP.NET WebForms)

/search is a WebForms page: GET ?keywords= is ignored and a replayed POST
returns an empty list (the postback needs the page's client-side state), so
the search is driven in headless Chromium: fill #tbKeywords, click
#btnSearch, bump #ddlPageRows to 100, then page with __doPostBack('Page',
'<n>'). The default "AI Best Match" mode is the only one that returns rows
(switching #rblSearchAI to plain keywords yields an empty list), so results
are relevance-ranked and noisy; title_ok does the filtering. Rows are div.listRow: a#lnkJobId (title, /job/
<key>/<slug>/<city>/<st>/<country>) and #companyLine spans
"<employer> - <City, ST, Country> - <MM/DD/YYYY>". Detail pages are plain
HTML with a JobPosting JSON-LD (description, datePosted, hiringOrganization).
"""
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "hbcucareers"
needs_browser = True
BASE = "https://www.hbcucareers.com"
QUERIES = ["adjunct computer", "adjunct information technology", "adjunct cybersecurity", "adjunct software",
           "instructor computer science", "lecturer computer science", "faculty computer science",
           "faculty cybersecurity", "faculty information systems", "adjunct data science"]
MAX_PAGES = 1          # 100 rows/page; page 2 of the relevance ranking repeats page 1
MAX_DETAILS = 150
DELAY = 1.0
UA = HEADERS["User-Agent"].replace("124.0.0.0", "130.0.0.0")


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _rows(html):
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("div.listRow"):
        a = row.select_one("a#lnkJobId[href]") or row.select_one(".title a[href]")
        if not a:
            continue
        spans = [clean(s.get_text(" ", strip=True)) for s in row.select("#companyLine span")]
        spans = [s for s in spans if s and s != "-"]
        posted = ""
        for s in spans:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
            if m:
                posted = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        loc = spans[1] if len(spans) > 1 and not re.fullmatch(r"[\d/]+", spans[1]) else ""
        yield {
            "title": clean(a.get_text(" ", strip=True)),
            "url": urljoin(BASE, a["href"]),
            "institution": spans[0] if spans else "",
            "location": re.sub(r",\s*United States$", "", loc),
            "posted": posted,
        }


def _search_pages(page, query):
    """Yield the HTML of each result page for one keyword query."""
    page.goto(f"{BASE}/search", wait_until="domcontentloaded", timeout=30000)
    page.fill("#tbKeywords", query)
    page.click("#btnSearch")
    page.wait_for_load_state("networkidle", timeout=30000)
    try:
        page.select_option("#ddlPageRows", "100")
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(1000)
    except Exception:
        pass
    yield page.content()
    for n in range(2, MAX_PAGES + 1):
        if f"__doPostBack('Page', '{n}')" not in page.content():
            break
        page.evaluate(f"__doPostBack('Page', '{n}')")
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(1000)
        yield page.content()


def _detail(session, url):
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return "", "", "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.get_text(), strict=False)
        except Exception:
            continue
        if not isinstance(j, dict) or j.get("@type") != "JobPosting":
            continue
        org = j.get("hiringOrganization") or {}
        addr = ((j.get("jobLocation") or {}).get("address") or {}) if isinstance(j.get("jobLocation"), dict) else {}
        loc = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x)
        return (strip_html(j.get("description") or ""), (j.get("datePosted") or "")[:10],
                clean(org.get("name", "")) if isinstance(org, dict) else "", loc)
    body = soup.select_one("#description, #lblDescription") or soup.find("main")
    return (strip_html(str(body)) if body else ""), "", "", ""


def fetch(config):
    out, seen, hits = [], set(), []
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            page.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|css)(\?|$)", re.I), lambda r: r.abort())
            for q in QUERIES:
                try:
                    for html in _search_pages(page, q):
                        new = 0
                        for row in _rows(html):
                            if row["url"] in seen:
                                continue
                            seen.add(row["url"])
                            new += 1
                            if title_ok(row["title"]):
                                hits.append(row)
                        if new == 0:
                            break
                except Exception:
                    continue
                time.sleep(DELAY)
            browser.close()
    except Exception:
        pass
    session = requests.Session()
    session.headers.update(HEADERS)
    for i, row in enumerate(hits):
        desc, posted, inst, loc = "", "", "", ""
        if i < MAX_DETAILS:
            try:
                desc, posted, inst, loc = _detail(session, row["url"])
            except Exception:
                pass
            time.sleep(DELAY)
        cfg = dict(config)
        cfg["name"] = inst or row["institution"] or cfg.get("name", "")
        try:
            out.append(make_job(cfg, "college_hbcucareers", row["title"], row["url"], loc or row["location"],
                                desc, posted or row["posted"]))
        except Exception:
            continue
    return out
