"""Madgex-powered higher-ed boards: Inside Higher Ed Careers and the Chronicle.

Both expose a public RSS search: <base>/jobsrss/?keywords=<q>&countrycode=US
(Inside Higher Ed also honours &sort=date). Entries carry title, link, the
hiring institution in <description>/<category>, and pubDate. Detail pages hold
the full description inside .job-description / .mds-content-content.
"""
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "madgex"
needs_browser = False

QUERIES = [
    "adjunct computer science", "adjunct information technology", "adjunct cybersecurity",
    "adjunct software", "adjunct data science", "adjunct programming", "adjunct computing",
    "instructor computer science", "instructor information technology", "instructor cybersecurity",
    "lecturer computer science", "lecturer cybersecurity", "faculty computer science",
    "faculty cybersecurity", "faculty information systems", "part-time faculty computer",
    "online instructor computer", "visiting professor computer science",
]
PAGES = 3  # 20 items per page
NON_US = re.compile(
    r"\b(China|Hong Kong|Lebanon|Saudi|Qatar|United Arab|Dubai|Abu Dhabi|Kuwait|Oman|Bahrain|Egypt|Jordan|"
    r"United Kingdom|England|Scotland|Wales|Ireland|Canada|Ontario|British Columbia|Quebec|Alberta|Manitoba|"
    r"Australia|New Zealand|Singapore|Malaysia|Japan|Korea|India|Pakistan|Bangladesh|Germany|France|Netherlands|"
    r"Belgium|Switzerland|Sweden|Norway|Denmark|Finland|Spain|Italy|Portugal|Kazakhstan|Uzbekistan|Vietnam|"
    r"Thailand|Indonesia|Philippines|Taiwan|Mexico|Brazil|Chile|Colombia|Peru|South Africa|Kenya|Nigeria|Ghana|"
    r"Rwanda|Turkey|Israel|Cyprus|Greece|Poland|Czech|Hungary|Austria|Morocco|Ethiopia|Uganda)\b"
    r"|\((CN|LB|GB|UK|AU|SG|JP|KR|IN|DE|FR|NL|SA|AE|QA|KW|HK|MY|NZ|MX|BR|ZA|TR|IL|IE|CH|SE|NO|DK|FI|ES|IT|PT|BE|AT|PL|KZ)\)", re.I)


def detect(url, html=""):
    if re.search(r"careers\.insidehighered\.com|jobs\.chronicle\.com", url):
        return {"url": re.match(r"https?://[^/]+", url).group(0)}
    return None


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        body = (soup.select_one(".mds-content-content, .job-description, #job-description, .job-detail-description")
                or soup.find("article") or soup.body)
        loc = ""
        for dt in soup.select("dt"):
            if "location" in dt.get_text(strip=True).lower():
                dd = dt.find_next("dd")
                if dd:
                    loc = clean(dd.get_text(" ", strip=True))
                    break
        return (strip_html(str(body)) if body else ""), loc
    except Exception:
        return "", ""


def fetch(config):
    base = config["url"].rstrip("/")
    out, seen = [], set()
    for q in QUERIES:
        for page in range(1, PAGES + 1):
            params = {"keywords": q, "countrycode": "US", "page": page}
            try:
                r = requests.get(f"{base}/jobsrss/?{urlencode(params)}", headers=HEADERS, timeout=25)
                if r.status_code != 200 or b"<rss" not in r.content[:300]:
                    break
                root = ET.fromstring(r.content)
            except Exception:
                break
            items = root.findall("./channel/item")
            if not items:
                break
            for it in items:
                title = clean(it.findtext("title") or "")
                url = re.sub(r"\?.*$", "", (it.findtext("link") or "").strip())
                inst_prefix = ""
                m0 = re.match(r"^(.{3,120}?):\s+(.{4,})$", title)
                if m0:                       # "Lone Star College: Adjunct Faculty, Computer Science"
                    inst_prefix, title = m0.group(1).strip(), m0.group(2).strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                if not title_ok(title):
                    continue
                desc_html = it.findtext("description") or ""
                # description is "<institution> - <location> - <summary>" style text
                desc_txt = strip_html(desc_html)
                inst = clean(it.findtext("author") or "") or clean((it.findtext("category") or ""))
                m = re.match(r"^(.*?)\s+[-–|]\s+(.*?)(?:\s+[-–|]\s+|$)", desc_txt)
                if not inst and m:
                    inst = m.group(1)[:120]
                loc = m.group(2)[:80] if m else ""
                pub = it.findtext("pubDate") or ""
                posted = ""
                for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
                    try:
                        posted = time.strftime("%Y-%m-%d", time.strptime(pub.strip(), fmt))
                        break
                    except Exception:
                        pass
                full, loc2 = _detail(url)
                location = loc2 or loc
                if NON_US.search(f"{location} {inst_prefix}"):
                    continue
                job = make_job(config, "college_madgex", title, url, location, full or desc_txt, posted)
                if inst_prefix or inst:
                    job.company = inst_prefix or inst
                out.append(job)
            if len(items) < 20:
                break
    return out
