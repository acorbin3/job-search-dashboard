"""PageUp People boards. Two flavours are seen at US colleges:

* "classic" PageUp:  https://careers.pageuppeople.com/<id>/cw/en-us/listing/
  (or a vanity host such as https://jobs.hr.uconn.edu/cw/en-us/listing/).
  /cw/en-us/rss returns *every* open posting with title, link, pubDate,
  job:location and the full job:description, so no detail fetch is needed.
  Fallback: paginate /cw/en-us/listing/?page=N and read #job-content.

* PageUp "Clinch" career sites: https://jobs.ucf.edu/jobs/search?query=<kw>&page=N
  (Rails app; assets on d25wby5c7p9100.cloudfront.net, cookie domains
  clinchtalent.com / career-pages.com). These sit behind an AWS WAF JavaScript
  challenge (HTTP 202 + challenge.js) for every path except robots/sitemap.
  Plain requests fail, so we open the board once in Playwright, wait for the
  challenge to issue the ``aws-waf-token`` cookie, then reuse that cookie jar
  with requests for listing and detail pages. Detail pages carry a
  schema.org JobPosting ld+json block with description/datePosted/jobLocation.
"""
import json
import re
import time
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "pageup"
needs_browser = True      # only for Clinch sites; classic boards use plain requests
ATS = "college_pageup"
QUERIES = ["adjunct", "instructor", "lecturer", "faculty", "professor"]
MAX_JOBS = 1000
MARKERS = re.compile(r"pageuppeople\.com|d25wby5c7p9100\.cloudfront|d2nc0na9w8r5aj\.cloudfront|"
                     r"clinchtalent\.com|career-pages\.com|job-search-results-card|/cw/en-us/", re.I)
_cookie_cache = {}        # host -> {cookie name: value}


def detect(url, html=""):
    u = url or ""
    if "pageuppeople.com" in u.lower() or "/cw/en-us/" in u.lower() or MARKERS.search(html or ""):
        p = urlsplit(u)
        base = f"{p.scheme}://{p.netloc}"
        m = re.match(r"(/\d+)/cw/", p.path)          # careers.pageuppeople.com/<id>/cw/...
        if m:
            base += m.group(1)
        return {"url": base}
    return None


def _is_challenge(r):
    return r.status_code == 202 or ("awswaf" in r.text[:4000] and "challenge" in r.text[:4000])


# --------------------------------------------------------------------------- classic
def _classic_rss(base):
    r = requests.get(f"{base}/cw/en-us/rss", headers=HEADERS, timeout=60)
    if r.status_code != 200 or b"<rss" not in r.content[:300]:
        return None
    root = ET.fromstring(r.content)
    jobs = []
    for it in root.iter("item"):
        d = {}
        for el in it:
            tag = el.tag.split("}")[-1]
            if "pageuppeople.com" in el.tag:      # <job:description>, <job:location>, ...
                tag = "job_" + tag
            d.setdefault(tag, el.text or "")
        d["description"] = d.get("job_description") or d.get("description", "")
        d["location"] = d.get("job_location") or d.get("location", "")
        jobs.append(d)
    return jobs


def _classic_detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        content = soup.find(id="job-content") or soup.find(id="job-details") or soup.body
        loc = ""
        m = re.search(r"Location:\s*([^\n]+?)\s*(?:Categories:|$)",
                      (soup.find(id="job-details") or soup).get_text("\n", strip=True))
        if m:
            loc = clean(m.group(1))
        return strip_html(str(content)) if content else "", loc
    except Exception:
        return "", ""


def _fetch_classic(config, base):
    out, seen = [], set()
    items = None
    try:
        items = _classic_rss(base)
    except Exception:
        items = None
    if items is not None:
        for d in items:
            title, url = clean(d.get("title")), (d.get("link") or "").strip()
            if not url or url in seen or not title_ok(title):
                continue
            seen.add(url)
            posted = (d.get("updated") or "")[:10]
            if not posted and d.get("pubDate"):
                try:
                    from email.utils import parsedate_to_datetime
                    posted = parsedate_to_datetime(d["pubDate"]).strftime("%Y-%m-%d")
                except Exception:
                    posted = ""
            desc = strip_html(d.get("description") or "") if d.get("description") else ""
            if len(desc) < 200:
                desc, _ = _classic_detail(url)
            out.append(make_job(config, ATS, title, url, clean(d.get("location")), desc, posted))
        return out
    # fallback: walk the paginated listing
    for page in range(1, 60):
        try:
            r = requests.get(f"{base}/cw/en-us/listing/", params={"page": page}, headers=HEADERS, timeout=25)
        except Exception:
            break
        links = [(a.get("href"), a.get_text(" ", strip=True))
                 for a in BeautifulSoup(r.text, "html.parser").select("a[href*='/job/']")]
        new = 0
        for href, title in links:
            url = urljoin(base + "/", href)
            if url in seen or not title:
                continue
            seen.add(url)
            new += 1
            if title_ok(title):
                desc, loc = _classic_detail(url)
                out.append(make_job(config, ATS, title, url, loc, desc, ""))
        if not new or len(seen) >= MAX_JOBS:
            break
    return out


# --------------------------------------------------------------------------- clinch
def _waf_cookies(base):
    """Run the AWS WAF challenge in a real browser and return the cookies."""
    host = urlsplit(base).netloc
    if host in _cookie_cache:
        return _cookie_cache[host]
    from playwright.sync_api import sync_playwright
    cookies = {}
    with sync_playwright() as p:
        browser = None
        for kw in ({"channel": "chrome"}, {}):
            try:
                browser = p.chromium.launch(headless=True, **kw)
                break
            except Exception:
                continue
        if browser is None:
            return {}
        try:
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            page.goto(f"{base}/jobs/search", timeout=60000)
            for _ in range(40):
                if "challenge-container" not in page.content() and page.title():
                    break
                time.sleep(1)
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        finally:
            browser.close()
    _cookie_cache[host] = cookies
    return cookies


def _clinch_session(base):
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(f"{base}/jobs/search", timeout=25)
    if _is_challenge(r):
        for k, v in _waf_cookies(base).items():
            s.cookies.set(k, v, domain=urlsplit(base).netloc)
        r = s.get(f"{base}/jobs/search", timeout=25)
        if _is_challenge(r):
            return None
    return s


def _clinch_detail(s, url):
    try:
        r = s.get(url, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        desc, loc, posted = "", "", ""
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                j = json.loads(sc.string or "")
            except Exception:
                continue
            if j.get("@type") != "JobPosting":
                continue
            desc = strip_html(j.get("description") or "")
            posted = (j.get("datePosted") or "")[:10]
            locs = j.get("jobLocation") or []
            if isinstance(locs, dict):
                locs = [locs]
            for l in locs:
                a = (l or {}).get("address") or {}
                loc = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion")) if x)
                if loc:
                    break
            break
        if not desc:
            main = soup.find("main") or soup.find(class_="page-body") or soup.body
            desc = strip_html(str(main)) if main else ""
        return desc, loc, posted
    except Exception:
        return "", "", ""


def _fetch_clinch(config, base):
    s = _clinch_session(base)
    if s is None:
        return []
    out, seen = [], set()
    for q in QUERIES:
        for page in range(1, 40):
            try:
                r = s.get(f"{base}/jobs/search", params={"query": q, "page": page}, timeout=30)
                if r.status_code != 200:
                    break
            except Exception:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("article.job-search-results-card-col")
            if not cards:
                break
            for c in cards:
                a = c.select_one("h3 a, .job-search-results-card-title a, a[href*='/jobs/']")
                if not a:
                    continue
                url, title = urljoin(base + "/", a.get("href", "")), clean(a.get_text(" ", strip=True))
                if url in seen or not title_ok(title):
                    continue
                seen.add(url)
                card_loc = c.select_one(".job-component-location span")
                card_loc = clean(card_loc.get_text(" ", strip=True)) if card_loc else ""
                desc, loc, posted = _clinch_detail(s, url)
                out.append(make_job(config, ATS, title, url, loc or card_loc, desc, posted))
            if len(seen) >= MAX_JOBS:
                return out
    return out


# --------------------------------------------------------------------------- nuxt
# Newer PageUp "system" career sites (e.g. careers.lctcs.edu, one board for all
# LCTCS colleges) are a Nuxt app: the landing page is server-rendered with every
# open job as a card
#     <div title="<title>"><p>..</p></div><div title="<college>"><p>..</p></div>
#     <div><p><span>City, ST</span></p></div> ... <a href="/jobs/<id>/<slug>">
# and /jobs/<id>/<slug> is server-rendered too, with "Work Type:", "Locations:",
# "Categories:" spans ahead of the body. Plain requests, no WAF.
_NUXT_JOB = re.compile(r"^/jobs/(\d+)/", re.I)


def _is_nuxt(html):
    return "__NUXT_DATA__" in html and bool(re.search(r'href="/jobs/\d+/', html))


def _nuxt_cards(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=_NUXT_JOB):
        url = urljoin(base + "/", a["href"])
        if url in seen:
            continue
        seen.add(url)
        title = clean((a.get("title") or "").split(" - ", 1)[-1]) if a.get("title", "").startswith("Job #") else ""
        btn = a.find("button")
        if not title and btn and (btn.get("title") or "").startswith("Job #"):
            title = clean(btn["title"].split(" - ", 1)[-1])
        college = loc = summary = ""
        row = a.find_parent("div")
        for _ in range(4):
            if row is None:
                break
            titled = [d for d in row.find_all("div", title=True, recursive=True) if d.get("title")]
            if titled:
                if not title:
                    title = clean(titled[0]["title"])
                if len(titled) > 1:
                    college = clean(titled[1]["title"])
                span = row.find("span")
                loc = clean(span.get_text(" ", strip=True)) if span else ""
                break
            row = row.find_parent("div")
        if row is not None:
            nxt = row.find_next_sibling("div")
            p = nxt.find("p") if nxt else None
            if p and not p.find("a"):
                summary = clean(p.get_text(" ", strip=True))
        if title:
            out.append({"title": title, "url": url, "college": college, "location": loc, "summary": summary})
    return out


def _nuxt_detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return "", "", ""
    if r.status_code != 200:
        return "", "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    fields = {}
    for sp in soup.find_all("span"):
        inner = sp.find("span")
        if inner and inner.get_text(strip=True).endswith(":"):
            k = inner.get_text(strip=True).rstrip(":").lower()
            v = clean(sp.get_text(" ", strip=True)[len(inner.get_text(strip=True)):])
            if v:
                fields[k] = v
    main = soup.find("main") or soup.body
    text = strip_html(str(main)) if main else ""
    # drop the chrome before the first field label
    i = text.find("Job no:")
    if i > 0:
        text = text[i:]
    loc = fields.get("locations", "")
    return text, loc, fields.get("closes", "")


def _fetch_nuxt(config, base, html):
    out = []
    for i, c in enumerate(_nuxt_cards(html, base)):
        if not title_ok(c["title"]):
            continue
        desc, loc, closes = "", "", ""
        if i < MAX_JOBS:
            desc, loc, closes = _nuxt_detail(c["url"])
            time.sleep(0.5)
        header = f"{c['title']}\nCollege: {c['college']}\n" + (f"Closes: {closes}\n" if closes else "") + "\n"
        job = make_job(config, ATS, c["title"], c["url"], c["location"] or loc, header + (desc or c["summary"]), "")
        if c["college"]:
            job.company = c["college"]
        out.append(job)
    return out


# --------------------------------------------------------------------------- entry
def fetch(config):
    try:
        base = config["url"].rstrip("/")
        base = re.sub(r"/cw/.*$", "", base)
        if "pageuppeople.com" in base or "/cw/" in config["url"]:
            return _fetch_classic(config, base)
        try:
            if _classic_rss(base) is not None:
                return _fetch_classic(config, base)
        except Exception:
            pass
        try:
            r = requests.get(base + "/", headers=HEADERS, timeout=40)
            if r.status_code == 200 and _is_nuxt(r.text):
                return _fetch_nuxt(config, base, r.text)
        except Exception:
            pass
        return _fetch_clinch(config, base)
    except Exception:
        return []
