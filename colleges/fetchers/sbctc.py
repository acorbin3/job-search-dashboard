"""Washington SBCTC "College System Jobs Search":
https://www.sbctc.edu/about/jobs/college-system-job-search

ASP.NET WebForms page the State Board runs for all 34 community and technical
colleges (each college's HR posts into it). Plain requests work:

    GET  the page                     -> __VIEWSTATE / __EVENTVALIDATION
    POST the same URL with
        JobSearchControl$ddlPositionType  (0 all, f faculty FT, t faculty PT, ...)
        JobSearchControl$lbDiscipline     (0 all, 024 Computer & Info Science,
                                           060 Information Technology, ...)
        JobSearchControl$lbCollege=0  JobSearchControl$ddlRegion=0
        JobSearchControl$btnSearch=Search
    -> #JobSearchControl_lblJobSearchResults "N" Jobs Found
       table#JobSearchControl_gvListOfJobs rows: College | Job Title (a[href] to
       the college's own board/posting) | Salary | Closing Date

The grid shows 20 rows per page; later pages need a __doPostBack
('JobSearchControl$gvListOfJobs','Page$N') replay, which we attempt and give up
on silently if the grid comes back empty. Discipline-filtered searches are
small enough to fit one page. Links are usually to a college's job *list*
rather than the posting, so no detail fetch; salary/closing from the row are
kept in the description.
"""
import re
import time

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, clean

name = "sbctc"
needs_browser = False
URL = "https://www.sbctc.edu/about/jobs/college-system-job-search"
# (position type, discipline) searches; disciplines: 024 Computer & Info Science,
# 060 Information Technology, 037 Electronics, 039 Engineering, 070 Math/Stats
SEARCHES = [("0", "024"), ("0", "060"), ("t", "0"), ("f", "0"), ("0", "037"), ("0", "039"), ("0", "070")]
MAX_PAGES = 10
DELAY = 1.0
PFX = "JobSearchControl$"


def detect(url, html=""):
    return None  # seeded manually as an aggregator


def _hidden(soup):
    return {i.get("name"): i.get("value", "") for i in soup.find_all("input", {"type": "hidden"}) if i.get("name")}


def _rows(soup):
    table = soup.select_one("#JobSearchControl_gvListOfJobs")
    if not table:
        return []
    out = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        a = tds[1].find("a", href=True)
        if not a:
            continue
        out.append({
            "college": clean(tds[0].get_text(" ", strip=True)),
            "title": clean(a.get_text(" ", strip=True)),
            "url": a["href"].strip(),
            "salary": clean(tds[2].get_text(" ", strip=True)),
            "closing": clean(tds[3].get_text(" ", strip=True)),
        })
    return out


def _count(soup):
    el = soup.select_one("#JobSearchControl_lblJobSearchResults")
    try:
        return int(clean(el.get_text())) if el else 0
    except ValueError:
        return 0


def _search(session, ptype, disc):
    r = session.get(URL, timeout=30)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    fields = {PFX + "ddlPositionType": ptype, PFX + "lbDiscipline": disc,
              PFX + "lbCollege": "0", PFX + "ddlRegion": "0"}
    data = {**_hidden(soup), **fields, PFX + "btnSearch": "Search"}
    time.sleep(DELAY)
    r = session.post(URL, data=data, timeout=40)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    rows = _rows(soup)
    total = _count(soup)
    page = 2
    while len(rows) < total and page <= MAX_PAGES:
        data = {**_hidden(soup), **fields, "__EVENTTARGET": PFX + "gvListOfJobs",
                "__EVENTARGUMENT": f"Page${page}"}
        time.sleep(DELAY)
        r = session.post(URL, data=data, timeout=40)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        more = _rows(soup)
        if not more or more[0]["url"] == rows[-1]["url"]:
            break
        rows.extend(more)
        page += 1
    return rows


def fetch(config):
    out, seen = [], set()
    session = requests.Session()
    session.headers.update(HEADERS)
    for ptype, disc in SEARCHES:
        try:
            rows = _search(session, ptype, disc)
        except Exception:
            continue
        for row in rows:
            key = (row["college"], row["title"], row["url"])
            if key in seen or not row["title"] or not title_ok(row["title"]):
                continue
            seen.add(key)
            # the grid carries the short college name ("Tacoma", "Spokane Colleges")
            college = row["college"] + ("" if re.search(r"college|district|sbctc|\bcis\b", row["college"], re.I) else " (WA CTC)")
            desc = (f"{row['title']}\nCollege: {row['college']} (Washington community/technical college)\nSalary: {row['salary']}\n"
                    f"Closing Date: {row['closing']}\nSource: SBCTC College System Jobs Search")
            try:
                job = make_job(config, "college_sbctc", row["title"], row["url"], f"{college}, WA", desc, "")
            except Exception:
                continue
            job.company = college
            out.append(job)
    return out
