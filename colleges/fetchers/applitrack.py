"""Frontline AppliTrack boards: https://www.applitrack.com/<district>/onlineapp/default.aspx?all=1

config: {"url": ..., "district": "<district>"}
default.aspx only carries categories; the postings are pulled by
jobpostings/Output.asp?all=1, a script of document.write('...') chunks whose
concatenated HTML holds one ul.postingsList per opening: table.title with
the title and "JobID: <n>", li label/value pairs (Position Type, Date
Posted, Location) and the full description in span#DescriptionText<n>_.
Verified against Tulsa Tech (also IVCC, Metro Tech, MNTC, ICTC, Lancaster CTC, West-MEC).
"""
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "applitrack"
needs_browser = False

DISTRICT_RE = re.compile(r"applitrack\.com/([a-z0-9_-]+)/onlineapp", re.I)
WRITE_RE = re.compile(r"document\.write\('((?:[^'\\]|\\.)*)'\)", re.S)


def detect(url, html=""):
    m = DISTRICT_RE.search(url or "") or DISTRICT_RE.search(html or "")
    if not m:
        return None
    d = m.group(1).lower()
    return {"url": f"https://www.applitrack.com/{d}/onlineapp/default.aspx?all=1", "district": d}


def _date(s):
    try:
        return datetime.strptime(clean(s), "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        return ""


def fetch(config):
    try:
        d = config.get("district") or (DISTRICT_RE.search(config["url"]) or [None, ""])[1]
        r = requests.get(f"https://www.applitrack.com/{d}/onlineapp/jobpostings/Output.asp?all=1",
                         headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        html = "".join(p.encode("utf-8", "surrogatepass").decode("unicode_escape") for p in WRITE_RE.findall(r.text))
        soup = BeautifulSoup(html, "html.parser")
        out, seen = [], set()
        for ul in soup.select("ul.postingsList"):
            td = ul.select_one("table.title td")
            title = clean(td.get_text(" ", strip=True)) if td else ""
            m = re.search(r"JobID:\s*(\d+)", ul.get_text(" ", strip=True))
            jid = m.group(1) if m else ""
            if not title or not jid or jid in seen or not title_ok(title):
                continue
            seen.add(jid)
            fields = {}
            for li in ul.find_all("li"):
                lab, val = li.find(class_="label"), li.find(class_="normal")
                if lab and val:
                    fields[clean(lab.get_text()).rstrip(":").lower()] = clean(val.get_text(" ", strip=True))
            desc_el = ul.find(id=re.compile(r"^DescriptionText"))
            out.append(make_job(config, "college_applitrack", title,
                                f"https://www.applitrack.com/{d}/onlineapp/default.aspx?AppliTrackJobID={jid}",
                                location=fields.get("location", ""),
                                description=strip_html(str(desc_el))[:20000] if desc_el else "",
                                posted=_date(fields.get("date posted", "")),
                                department=fields.get("position type", "")))
        return out
    except Exception:
        return []
