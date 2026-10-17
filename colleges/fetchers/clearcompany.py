"""ClearCompany boards, legacy hrmdirect flavour:
  https://<slug>.hrmdirect.com/employment/job-openings.php?search=true

config: {"url": ..., "cc_slug": "<slug>"}
The openings page is a server-rendered table (tr.reqitem with the title link
job-opening.php?req=<id>, department and category columns); the detail page
carries the body in div.jobDesc. A <slug>.clearcompany.com board / embed is
mapped onto the same hrmdirect host (the tenants share the slug); the
newer React career site has no public JSON and is not handled.
Verified against Oral Roberts University.
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "clearcompany"
needs_browser = False

SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.(?:hrmdirect|clearcompany)\.com", re.I)


def detect(url, html=""):
    m = SLUG_RE.search(url or "") or SLUG_RE.search(html or "")
    if not m or m.group(1).lower() in ("www", "cc-app-static", "cc-webapp-static", "app", "api"):
        return None
    slug = m.group(1).lower()
    return {"url": f"https://{slug}.hrmdirect.com/employment/job-openings.php?search=true", "cc_slug": slug}


def _detail(sess, url):
    try:
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            return ""
        box = BeautifulSoup(r.text, "html.parser").find("div", class_="jobDesc")
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        slug = config.get("cc_slug") or (SLUG_RE.search(config["url"]) or [None, ""])[1]
        url = f"https://{slug}.hrmdirect.com/employment/job-openings.php?search=true"
        sess = requests.Session()
        sess.headers.update(HEADERS)
        r = sess.get(url, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for tr in soup.select("tr.reqitem"):
            a = tr.find("a", href=re.compile(r"job-opening\.php\?req=\d+", re.I))
            if not a:
                continue
            title = clean(a.get_text(" ", strip=True))
            m = re.search(r"req=(\d+)", a["href"])
            rid = m.group(1)
            if not title or rid in seen or not title_ok(title):
                continue
            seen.add(rid)
            href = urljoin(url, f"job-opening.php?req={rid}")
            dept_el = tr.find("td", class_="departments")
            dept = clean(dept_el.get_text(" ", strip=True)) if dept_el else ""
            out.append(make_job(config, "college_clearcompany", title, href,
                                description=_detail(sess, href), department=dept))
        return out
    except Exception:
        return []
