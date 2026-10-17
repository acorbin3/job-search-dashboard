"""Hirezon / Interview Exchange boards (Northeast colleges):
https://<school>.interviewexchange.com/static/clients/<id>M/index.jsp

The HTML index and jobofferdetails.jsp pages sit behind a Cloudflare JS
challenge plus a WAF that 403s repeated detail hits, but every board also
publishes an RSS feed that plain requests can read:
  https://<school>.interviewexchange.com/jobsrss.jsp?COMPANYID=<n>
where <n> is the leading digits of the client id in the board URL
(349NEM1 -> 349). Each <item> carries title, link, location, department,
pubDate, salary and a (truncated, ~900 char) description, so no detail
fetch is needed. If the id can't be derived, Playwright is used once to open
the index and read the feed link from the page.
Verified against Northern Essex CC (necc / 349NEM1), Bristol CC, Quincy College.
"""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urljoin
from email.utils import parsedate_to_datetime

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean, BROWSER_UA

name = "interviewexchange"
needs_browser = False
MAX_JOBS = 1000


def detect(url, html=""):
    if "interviewexchange.com" in urlsplit(url).netloc.lower():
        return {"url": url, "company_id": _company_id(url)}
    m = re.search(r"https?://[a-z0-9-]+\.interviewexchange\.com/static/clients/[^\"'\s<>]+", html or "", re.I)
    if m:
        return {"url": m.group(0), "company_id": _company_id(m.group(0))}
    return None


def _company_id(url):
    m = re.search(r"COMPANYID=(\d+)", url, re.I) or re.search(r"/static/clients/(\d+)", url)
    return m.group(1) if m else ""


def _origin(url):
    p = urlsplit(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def _feed_url_via_browser(url):
    """Last resort: open the index in Playwright and read the jobsrss link."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context(user_agent=BROWSER_UA).new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            html = ""
            for _ in range(12):
                html = page.content()
                if "jobsrss.jsp" in html:
                    break
                page.wait_for_timeout(1500)
            browser.close()
        m = re.search(r"jobsrss\.jsp\?COMPANYID=(\d+)", html)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _date(s):
    try:
        return parsedate_to_datetime(clean(s)).strftime("%Y-%m-%d")
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", s or "")
        return m.group(0) if m else ""


def _description(raw):
    txt = strip_html(raw or "")
    # The feed prefixes the body with "Location: ... Posted On: ... Job Description:".
    m = re.search(r"Job Description:\s*", txt)
    return txt[m.end():].strip() if m else txt


def fetch(config):
    try:
        url = config["url"]
        cid = config.get("company_id") or _company_id(url) or _feed_url_via_browser(url)
        if not cid:
            return []
        r = requests.get(f"{_origin(url)}/jobsrss.jsp", params={"COMPANYID": cid},
                         headers=dict(HEADERS, Accept="*/*"), timeout=30)
        if r.status_code != 200 or b"<rss" not in r.content[:300]:
            return []
        root = ET.fromstring(r.content)
        school = clean((root.findtext("./channel/title") or "").split(":::")[0])
        out, seen = [], set()
        for it in root.findall(".//item"):
            title = clean(it.findtext("title") or "")
            # Titles are suffixed " - <College Name>" (the feed's channel title).
            for suffix in (school, config.get("name") or ""):
                if suffix and title.lower().endswith(" - " + suffix.lower()):
                    title = title[: -len(suffix) - 3].rstrip()
            link = clean(it.findtext("link") or "")
            m = re.search(r"JOBID=(\d+)", link or (it.findtext("guid") or ""))
            jid = m.group(1) if m else link
            if not jid or jid in seen or not title_ok(title):
                continue
            seen.add(jid)
            link = urljoin(_origin(url), f"/jobofferdetails.jsp?JOBID={jid}") if m else link
            out.append(make_job(config, "college_interviewexchange", title, link,
                                clean(it.findtext("location") or ""),
                                _description(it.findtext("description")),
                                _date(it.findtext("pubDate") or ""),
                                clean(it.findtext("department") or "")))
            if len(out) >= MAX_JOBS:
                break
        return out
    except Exception:
        return []
