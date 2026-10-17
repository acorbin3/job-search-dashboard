"""PowerSchool Applicant Tracking Enterprise (school districts, technical colleges):
  https://<host>.atenterprise.powerschool.com/ats/job_board?COMPANY_ID=<id>

config: {"url", "host": "ats3.atenterprise.powerschool.com", "company_id": "JA001601"}
The board is a server-rendered table: the header row names the columns
(Application Type / Location or Building/Department / Job Title) and every
row links job_board_form?op=view&JOB_ID=<n>; a "1-100 / 101-200" table links
start_index=<n> pages. The detail page carries div.job-name, div.job-address,
div.job-description and div.job-items (Open Date, Salary, Additional Job
Information). The job_board_frame?APPLICANT_TYPE_ID= variant of the URL is an
empty shell, so detect() normalises it to job_board?COMPANY_ID=.
Verified against Leon County Schools (JA001601, 111 jobs) and Hernando (00005595).
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "powerschool"
needs_browser = False
URL_RE = re.compile(r"https?://([a-z0-9-]+\.atenterprise\.powerschool\.com)/ats/job_board[^\s\"'<>]*?"
                    r"COMPANY_ID=([A-Za-z0-9]+)", re.I)
MAX_PAGES = 20


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search((html or "").replace("&amp;", "&"))
    if not m:
        return None
    host, cid = m.group(1).lower(), m.group(2)
    return {"url": f"https://{host}/ats/job_board?COMPANY_ID={cid}", "host": host, "company_id": cid}


def _detail(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return "", "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        loc = clean((soup.find("div", class_="job-address") or soup.new_tag("i")).get_text(" ", strip=True))
        posted = ""
        od = soup.find("div", class_="job-open-date")
        if od:
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", od.get_text())
            posted = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""
        parts = [el.get_text(" ", strip=True) for el in soup.find_all("div", class_=("job-description", "job-items"))]
        return strip_html("\n".join(parts))[:20000], loc, posted
    except Exception:
        return "", "", ""


def fetch(config):
    try:
        host, cid = config["host"], config["company_id"]
        base = f"https://{host}/ats/job_board?COMPANY_ID={cid}"
        out, seen, todo, done = [], set(), [base], set()
        while todo and len(done) < MAX_PAGES:
            url = todo.pop(0)
            done.add(url)
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=re.compile(r"start_index=\d+")):
                u = urljoin(base, a["href"].replace("&amp;", "&"))
                if u not in done and u not in todo:
                    todo.append(u)
            for a in soup.find_all("a", href=re.compile(r"job_board_form\?op=view", re.I)):
                tr = a.find_parent("tr")
                table = tr.find_parent("table") if tr else None
                if not tr or not table:
                    continue
                hdr = [clean(c.get_text(" ", strip=True)) for c in (table.find("tr") or tr).find_all(["th", "td"])]
                cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all("td")]
                jid = re.search(r"JOB_ID=(\d+)", a["href"]).group(1)
                if jid in seen:
                    continue

                def col(label):
                    for i, h in enumerate(hdr):
                        if label in h.lower() and i < len(cells):
                            return cells[i]
                    return ""
                title = col("job title") or col("title") or (cells[-1] if cells else "")
                if not title or not title_ok(title):
                    continue
                seen.add(jid)
                href = f"https://{host}/ats/job_board_form?op=view&JOB_ID={jid}&COMPANY_ID={cid}"
                body, loc, posted = _detail(href)
                dept = col("location") or col("building") or col("department")
                out.append(make_job(config, "college_powerschool", title, href,
                                    location=loc or dept, description=body, posted=posted,
                                    department=dept))
        return out
    except Exception:
        return []
