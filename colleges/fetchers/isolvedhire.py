"""isolved Hire boards (ex-ApplicantPro) and its white-label hosts, which all
share the /jobs/ page and /core/jobs/<domainId> JSON:
  https://<slug>.isolvedhire.com/jobs/   (also applicantpool.com, applicantlist.com,
  workbrightats.com, aaimtrack.com)

config: {"url": "https://<slug>.<host>/jobs/", "isolved_slug": "<slug>", "host": "<slug>.<host>"}
isolvedhire.com sometimes answers 503 to plain requests (F5 / bot wall) so Playwright with
--disable-blink-features=AutomationControlled is the fallback, the same way
interviewexchange.py falls back. Plain requests are tried first; when the
page comes back it carries a domainId and /core/jobs/<domainId> returns the
list JSON (as the applicantpro fetcher in scrape.py reads); otherwise the
rendered HTML is parsed for /jobs/<id> links. Job pages carry a JSON-LD
JobPosting with the description.
Verified against Converse University (isolvedhire), Loras (applicantpool),
St. Ambrose (applicantlist), Covenant (workbrightats) and St. Charles CC (aaimtrack).
"""
import html as _html
import json
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean, BROWSER_UA

name = "isolvedhire"
needs_browser = True
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.(isolvedhire|applicantpool|applicantlist|workbrightats|aaimtrack|mysmarthire)\.com", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() in ("www", "app"):
        return None
    slug, host = m.group(1).lower(), f"{m.group(1).lower()}.{m.group(2).lower()}.com"
    return {"url": f"https://{host}/jobs/", "isolved_slug": slug, "host": host}


def _ld_description(html):
    for s in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try:
            d = json.loads(s.string or "")
        except Exception:
            continue
        for item in (d if isinstance(d, list) else [d]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return strip_html(item.get("description") or "")[:20000]
    return ""


def _browser_pages(url, detail_urls_fn):
    """Open the listing (and each wanted detail page) in stealth Chromium."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        ctx = browser.new_context(user_agent=BROWSER_UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        html = page.content()
        details = {}
        for u in detail_urls_fn(html):
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(800)
                details[u] = page.content()
            except Exception:
                details[u] = ""
        browser.close()
    return html, details


def _parse_list(html, base):
    """(title, url, location) triples from the rendered /jobs/ page."""
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"/jobs/\d+", re.I)):
        title = clean(a.get_text(" ", strip=True))
        href = urljoin(base, a["href"]).split("?")[0]
        if not title or href in seen:
            continue
        seen.add(href)
        parent = a.find_parent(["li", "tr", "div"])
        loc_el = parent.find(class_=re.compile("location|city", re.I)) if parent else None
        rows.append((title, href, clean(loc_el.get_text(" ", strip=True)) if loc_el else ""))
    return rows


def fetch(config):
    try:
        m = SLUG_RE.search(config["url"])
        host = config.get("host") or (f"{m.group(1)}.{m.group(2)}.com" if m else "")
        base = f"https://{host}"
        url = f"{base}/jobs/"
        out = []
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200 and "notset.php" not in r.url:
            ids = re.findall(r"domainId[\"']?\s*[:=]\s*[\"']?(\d+)", r.text)
            jobs = []
            if ids:
                r2 = requests.get(f"{base}/core/jobs/{ids[0]}?getParams=%7B%7D", headers=HEADERS, timeout=25)
                if r2.status_code == 200 and r2.text.lstrip().startswith("{"):
                    jobs = ((r2.json() or {}).get("data") or {}).get("jobs") or []
            rows = [(clean(_html.unescape(j.get("title") or "")), j.get("jobUrl") or f"{base}/jobs/{j.get('id', '')}",
                     ", ".join(str(x) for x in (j.get("city"), j.get("abbreviation")) if x))
                    for j in jobs] or _parse_list(r.text, base)
            for title, href, loc in rows:
                if not title_ok(title):
                    continue
                desc = ""
                try:
                    rd = requests.get(href, headers=HEADERS, timeout=20)
                    if rd.status_code == 200:
                        desc = _ld_description(rd.text)
                except requests.RequestException:
                    pass
                out.append(make_job(config, "college_isolvedhire", title, href, location=loc, description=desc))
            return out
        # Bot wall: render in Chromium, then open only the matching detail pages.
        wanted = {}

        def _wanted(html):
            for title, href, loc in _parse_list(html, base):
                if title_ok(title):
                    wanted[href] = (title, loc)
            return list(wanted)

        _, details = _browser_pages(url, _wanted)
        for href, (title, loc) in wanted.items():
            out.append(make_job(config, "college_isolvedhire", title, href, location=loc,
                                description=_ld_description(details.get(href) or "")))
        return out
    except Exception:
        return []
