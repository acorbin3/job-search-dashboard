"""SilkRoad / Rival OpenHire boards:
  https://<tenant>-openhire.silkroad.com/epostings/index.cfm?version=1&company_id=<id>

config: {"url": ..., "host": "<tenant>-openhire.silkroad.com", "company_id": "<id>"}
index.cfm?fuseaction=app.allpositions lists every opening server-side:
div.cssAllJobListParentTitle (department) followed by div.cssAllJobListPosition
holding the a.cssAllJobListPositionHref title link (jobid=<n>) and the
location text. The submit.cfm?fuseaction=app.jobinfo page carries the body.

The newer portal https://jobs.silkroad.com/<tenant>/<site> (site =
ExternalCareerSite, External, Careers, EmploymentPortal ...):
config: {"url": "https://jobs.silkroad.com/<tenant>/<site>", "host": "jobs.silkroad.com",
         "tenant": "<tenant>", "site": "<site>"}
/<tenant>/<site>/sitemap lists every opening as li > a[href=/<site>/jobs/<id>]
(capped at 100, so a full sitemap falls back to walking /<site>?page=N,
10 a.sr-panel per page). The /jobs/<id>?embedded=true page (the bare one
also behind the school-site wrapper Shenandoah redirects to) carries either
#Jobs_JobDetail_JobDescriptionText / _LocationText / _CategoryText /
_JobPositionTypeText (newer sites) or #ConfigurablePageDetail__JobDescription /
__RequiredSkills / __DisplayLocation / __DisplayCategory / __PostingDate.
Verified against College of Southern Idaho (OpenHire; also Muskegon, SC4) and
St. Clair County CC (stclairccc/ExternalCareerSite, 55 jobs), Cameron
(Cameron/Careers), Shenandoah (EmploymentPortal) and Boston University (BU/External).
"""
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "silkroad"
needs_browser = False

URL_RE = re.compile(r"https?://([a-z0-9.-]+\.silkroad\.com)/epostings/[^\"'\s<>]*company_id=(\d+)", re.I)
PORTAL_RE = re.compile(r"https?://jobs\.silkroad\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)", re.I)
PORTAL_SKIP = {"errorhandler", "account", "api", "content", "scripts", "styles"}
MAX_PAGES = 100


def detect(url, html=""):
    h = (html or "").replace("&amp;", "&")
    m = URL_RE.search(url or "") or URL_RE.search(h)
    if m:
        host, cid = m.group(1).lower(), m.group(2)
        return {"url": f"https://{host}/epostings/index.cfm?fuseaction=app.allpositions&company_id={cid}&version=1",
                "host": host, "company_id": cid}
    m = PORTAL_RE.search(url or "") or PORTAL_RE.search(h)
    if m and m.group(1).lower() not in PORTAL_SKIP and m.group(2).lower() not in PORTAL_SKIP:
        tenant, site = m.group(1), m.group(2)
        return {"url": f"https://jobs.silkroad.com/{tenant}/{site}", "host": "jobs.silkroad.com",
                "tenant": tenant, "site": site}
    return None


def _portal_detail(url):
    """(body, location, category, posted) from a portal job page."""
    try:
        r = requests.get(url, params={"embedded": "true"}, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return "", "", "", ""
        soup = BeautifulSoup(r.text, "html.parser")

        def grab(*ids, label=""):
            for i in ids:
                el = soup.find(id=i)
                if el:
                    return clean(re.sub(rf"^{label}\s*", "", el.get_text(" ", strip=True), flags=re.I) if label else el.get_text(" ", strip=True))
            return ""
        parts = [soup.find(id=i) for i in ("Jobs_JobDetail_JobDescriptionText", "ConfigurablePageDetail__JobDescription",
                                           "ConfigurablePageDetail__RequiredSkills")]
        body = "\n".join(strip_html(str(x)) for x in parts if x) or strip_html(str(soup.find(class_="sr-job-detail__description") or ""))
        ptype = grab("Jobs_JobDetail_JobPositionTypeText", "ConfigurablePageDetail__DisplayPosition", label="Position Type")
        if ptype:
            body = f"Position type: {ptype}\n{body}"
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", grab("ConfigurablePageDetail__PostingDate"))
        posted = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""
        return (body[:20000], grab("Jobs_JobDetail_LocationText", "ConfigurablePageDetail__DisplayLocation", label="Job Location"),
                grab("Jobs_JobDetail_CategoryText", "ConfigurablePageDetail__DisplayCategory", label="Category"), posted)
    except Exception:
        return "", "", "", ""


def _portal_links(config):
    """(title, url) pairs from the sitemap, or from the paged list when the sitemap is capped."""
    base = f"https://jobs.silkroad.com/{config['tenant']}/{config['site']}"
    job_re = re.compile(rf"/{re.escape(config['site'])}/jobs/(\d+)", re.I)
    found = {}
    r = requests.get(base + "/sitemap", headers=HEADERS, timeout=25)
    if r.status_code == 200:
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=job_re):
            found.setdefault(job_re.search(a["href"]).group(1), clean(a.get_text(" ", strip=True)))
    if len(found) >= 100 or not found:
        found = {}
        for page in range(1, MAX_PAGES + 1):
            r = requests.get(base, params={"page": page}, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                break
            new = 0
            for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=job_re):
                jid = job_re.search(a["href"]).group(1)
                t = a.find(class_="sr-panel__title")
                if jid not in found:
                    found[jid] = clean((t or a).get_text(" ", strip=True))
                    new += 1
            if not new:
                break
    return [(title, f"{base}/jobs/{jid}") for jid, title in found.items()]


def _fetch_portal(config):
    out = []
    for title, href in _portal_links(config):
        if not title or not title_ok(title):
            continue
        body, loc, dept, posted = _portal_detail(href)
        out.append(make_job(config, "college_silkroad", title, href, location=loc, description=body,
                            posted=posted, department=dept))
    return out


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "select", "input", "button"]):
            t.decompose()
        for t in soup.find_all(class_="ui-dialog"):
            t.decompose()
        box = soup.find(id="box_col_center_L_Col") or soup.body
        return strip_html(str(box))[:20000] if box else ""
    except Exception:
        return ""


def fetch(config):
    try:
        if config.get("tenant") or PORTAL_RE.search(config.get("url") or ""):
            if not config.get("tenant"):
                config = dict(config, **detect(config["url"]))
            return _fetch_portal(config)
        host = config.get("host") or urlsplit(config["url"]).netloc
        cid = config.get("company_id") or (URL_RE.search(config["url"]) or [None, None, ""])[2]
        url = f"https://{host}/epostings/index.cfm?fuseaction=app.allpositions&company_id={cid}&version=1"
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen, dept = [], set(), ""
        for el in soup.find_all("div", class_=re.compile(r"cssAllJobList(ParentTitle|Position)$")):
            if "cssAllJobListParentTitle" in el.get("class", []):
                dept = clean(el.get_text(" ", strip=True))
                continue
            a = el.find("a", href=re.compile(r"jobid=\d+", re.I))
            if not a:
                continue
            title = re.sub(r"\s*\(\d+-\d+\)\s*$", "", clean(a.get_text(" ", strip=True)))
            jid = re.search(r"jobid=(\d+)", a["href"], re.I).group(1)
            if not title or jid in seen or not title_ok(title):
                continue
            seen.add(jid)
            href = urljoin(url, a["href"].replace("&amp;", "&"))
            loc = clean(el.get_text(" ", strip=True).replace(a.get_text(" ", strip=True), ""))
            out.append(make_job(config, "college_silkroad", title, href, location=loc,
                                description=_detail(href), department=dept))
        return out
    except Exception:
        return []
