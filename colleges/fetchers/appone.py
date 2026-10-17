"""AppOne / Paychex Flex recruiting boards:
  https://www2.appone.com/Search/Search.aspx?ServerVar=<slug>.appone.com

config: {"url": ..., "server_var": "<slug>.appone.com", "client_id": "26368"}
Search.aspx is an ASP.NET form, but its HTML carries the Paychex ClientID and
the "Browse All Jobs by Category" page on recruiting.myapps.paychex.com is a
plain server-rendered list: MainInfoReq.asp?R_ID=<id> links whose text is
"<title> - <ST, City> - Job". The MainInfoReq page carries the posting body.
Verified against Saint Vincent College.
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "appone"
needs_browser = False

SV_RE = re.compile(r"ServerVar=([a-z0-9-]+\.appone\.com)", re.I)
HOST_RE = re.compile(r"https?://([a-z0-9-]+)\.appone\.com", re.I)
BROWSE = ("https://recruiting.myapps.paychex.com/appone/Branding/ReqTemplate/BrowseAllJobsbyCategory.asp"
          "?Type=REQ&FromID=1&ClientID={cid}&B_ID={bid}&JobCode=0&CountryID=3&LanguageID=2&servervar={sv}")


def detect(url, html=""):
    blob = (url or "") + " " + (html or "")
    m = SV_RE.search(blob)
    sv = m.group(1).lower() if m else ""
    if not sv:
        m = HOST_RE.search(url or "")
        if m and m.group(1).lower() not in ("www", "www2", "www3", "recruiting"):
            sv = f"{m.group(1).lower()}.appone.com"
    if not sv:
        return None
    return {"url": f"https://www2.appone.com/Search/Search.aspx?ServerVar={sv}", "server_var": sv}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "form", "select"]):
            t.decompose()
        return strip_html(str(soup.body or soup))[:20000]
    except Exception:
        return ""


def fetch(config):
    try:
        sv = config.get("server_var") or (SV_RE.search(config["url"]) or [None, ""])[1]
        url = f"https://www2.appone.com/Search/Search.aspx?ServerVar={sv}"
        cid, bid = config.get("client_id"), config.get("branding_id")
        if not cid:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                return []
            cids = re.findall(r"ClientID=(\d+)", r.text)
            bids = re.findall(r"B_ID=(\d+)", r.text) or re.findall(r"txtBrandingId[^>]*value=\"(\d+)\"", r.text)
            if not cids:
                return []
            cid, bid = cids[0], (bid or (bids[0] if bids else "1"))
        r = requests.get(BROWSE.format(cid=cid, bid=bid or "1", sv=sv), headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"MainInfoReq\.asp\?R_ID=\d+", re.I)):
            text = clean(a.get_text(" ", strip=True))
            m = re.match(r"^(.*?)\s+-\s+([A-Z]{2},\s*[^-]+?)\s+-\s+Job$", text)
            title, loc = (m.group(1), m.group(2)) if m else (re.sub(r"\s+-\s+Job$", "", text), "")
            if loc and "," in loc:
                st, city = [x.strip() for x in loc.split(",", 1)]
                loc = f"{city}, {st}"
            rid = re.search(r"R_ID=(\d+)", a["href"]).group(1)
            if not title or rid in seen or not title_ok(title):
                continue
            seen.add(rid)
            href = urljoin(r.url, a["href"])
            out.append(make_job(config, "college_appone", title, href, location=loc,
                                description=_detail(href)))
        return out
    except Exception:
        return []
