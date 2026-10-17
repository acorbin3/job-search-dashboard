"""Jenzabar JICS "Employment Position Application" portlets:
  https://<portal>/ICS/<path>/<Page>.jnz

config: {"url": "https://<portal>/ICS/.../<Page>.jnz"}
The ASP.NET page lists nothing until the "Search positions" button is
posted (form#MAINFORM with its __VIEWSTATE fields plus
pgN$V$psSearch$gbtnSearch=Search positions); the reply holds
table[id*=gvPositions] (Position / Department / Classification / Type /
Posted) whose title links are __doPostBack('pgN$V$psSearch$gvPositions$ctlNN$ctl00')
postbacks that render the posting body on the same page. The portal keeps
the viewstate server-side and the detail view has no search button, so each
posting costs a fresh GET + search POST + detail postback.
Verified against Upper Iowa University, Angelina College and Rogers State.
"""
import re

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "jics"
needs_browser = False
URL_RE = re.compile(r"https?://[a-z0-9.-]+/ICS/[^\s\"'<>?#]*\.jnz", re.I)
POSTBACK_RE = re.compile(r"__doPostBack\('([^']+gvPositions[^']+)'")
MAX_DETAILS = 150


def detect(url, html=""):
    h = html or ""
    m = URL_RE.search(url or "")
    if not m or "psSearch" not in h:
        return None
    return {"url": m.group(0)}


def _form(soup):
    f = soup.find("form", id="MAINFORM") or soup.find("form")
    if not f:
        return None, {}
    data = {}
    for i in f.find_all("input"):
        if i.get("name") and i.get("type") not in ("submit", "button", "checkbox", "image"):
            data[i["name"]] = i.get("value", "")
    for sel in f.find_all("select"):
        opt = sel.find("option", selected=True)
        if sel.get("name"):
            data[sel["name"]] = opt.get("value", "") if opt else ""
    return f, data


def _detail_text(html, title):
    """Body of a posting page: from the title heading ('<title> Posted on ...') to the footer."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "select", "input", "button"]):
        t.decompose()
    text = strip_html(str(soup.body or soup))
    j = text.find("Posted on")
    i = text.rfind(title, 0, j) if j >= 0 else text.find(title)
    if i < 0:
        i = max(j, 0)
    text = re.split(r"\n\s*(?:Privacy policy|Powered by Jenzabar)", text[i:], 1)[0]
    return text[:20000]


def _search(s, url, soup):
    """POST 'Search positions' from the given page's form; returns the results soup (or None)."""
    f, data = _form(soup)
    btn = next((k for k in data if k.endswith("$psSearch$gbtnSearch")), None) or \
        next((b.get("name") for b in (f.find_all(["input", "button"]) if f else []) if "gbtnSearch" in (b.get("name") or "")), None)
    if not btn:
        return None
    data[btn] = "Search positions"
    r = s.post(url, data=data, timeout=40)
    return BeautifulSoup(r.text, "html.parser") if r.status_code == 200 else None


def fetch(config):
    try:
        url = config["url"]
        s = requests.Session()
        s.headers.update(HEADERS)
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            return []
        soup = _search(s, url, BeautifulSoup(r.text, "html.parser"))
        table = soup.find("table", id=re.compile("gvPositions")) if soup else None
        if not table:
            return []
        hdr = [clean(c.get_text(" ", strip=True)).lower() for c in table.find("tr").find_all(["th", "td"])]

        def col(cells, label):
            for i, h in enumerate(hdr):
                if label in h and i < len(cells):
                    return cells[i]
            return ""
        rows = []
        for tr in table.find_all("tr"):
            a = tr.find("a", href=POSTBACK_RE)
            if not a:
                continue
            cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all("td")]
            title = clean(a.get_text(" ", strip=True))
            if title and title_ok(title):
                rows.append((title, POSTBACK_RE.search(a["href"]).group(1), cells))
        out = []
        for n, (title, target, cells) in enumerate(rows):
            body = ""
            if n < MAX_DETAILS:
                try:
                    if n:                                   # fresh results viewstate for every postback
                        soup = _search(s, url, BeautifulSoup(s.get(url, timeout=30).text, "html.parser"))
                    _, pdata = _form(soup)
                    pdata["__EVENTTARGET"], pdata["__EVENTARGUMENT"] = target, ""
                    rd = s.post(url, data=pdata, timeout=40)
                    body = _detail_text(rd.text, title) if rd.status_code == 200 else ""
                except Exception:
                    body = ""
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", col(cells, "posted"))
            dept, typ = col(cells, "department"), col(cells, "type")
            if typ:
                body = f"Type: {typ}\n{body}"
            out.append(make_job(config, "college_jics", title, url, location="", description=body,
                                posted=f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else "",
                                department=dept))
        return out
    except Exception:
        return []
