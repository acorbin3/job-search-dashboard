#!/usr/bin/env python3
"""Find every US college's job board and classify its ATS.

    python3 -m colleges.discover                # all IPEDS institutions (resumes)
    python3 -m colleges.discover --limit 50     # smoke test
    python3 -m colleges.discover --redo         # ignore previous results
    python3 -m colleges.discover --states FL GA # subset
    python3 -m colleges.discover --build-only   # just regenerate data/colleges.json
    python3 -m colleges.discover --redo-unclassified --browser --workers 10
                                                # second pass in headless Chromium
    python3 -m colleges.discover --inherit-only # share boards across system campuses

For each institution: fetch the homepage, follow links that look like
jobs/careers/employment/HR (plus jobs./careers. subdomains and /jobs, /careers,
/employment paths), and run the ATS detectors on each landing page and on any
ATS-looking links found there. Writes data/colleges_registry.json as it goes
and data/colleges.json at the end.
"""
import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from colleges import institutions, registry  # noqa: E402
from colleges.fetchers import detect as detect_college  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
TIMEOUT = 15

JOB_LINK = re.compile(r"\b(jobs?|careers?|employment|job openings|job opportunities|work (at|with|for) |"
                      r"join (our|the) team|now hiring|open positions|position openings|"
                      r"career opportunities|employment opportunities|vacancies|faculty positions|"
                      r"human resources|\bhr\b)\b", re.I)
STRONG = re.compile(r"\b(jobs?|careers?|employment|openings|opportunit|positions|hiring|vacanc)", re.I)
ATS_HOST = re.compile(
    r"myworkdayjobs\.com|peopleadmin\.com|/postings/search|pageuppeople\.com|governmentjobs\.com|"
    r"schooljobs\.com|interfolio\.com|oraclecloud\.com|taleo\.net|icims\.com|csod\.com|successfactors|"
    r"ultipro\.com|ukg\.com|interviewexchange\.com|applicantpro\.com|paycomonline|paylocity\.com|"
    r"adp\.com|jobapscloud|applitrack|bamboohr\.com|greenhouse\.io|lever\.co|applytojob\.com|workable\.com|"
    r"dayforcehcm|smartrecruiters|jobvite\.com|hirezon|brassring|kenexa|silkroad|hiretouch|"
    r"peoplesoft|/psc/|/psp/|HRS_CG_SEARCH|recruitingbypaycor|paycor\.com/career|tbe\.taleo\.net|breezy\.hr|jobscore\.com|"
    r"hrmdirect\.com|clearcompany\.com|isolvedhire\.com|applicantpool\.com|applicantlist\.com|workbrightats\.com|aaimtrack\.com|mysmarthire\.com|"
    r"applicantstack\.com|apscareerportal\.com|saashr\.com|entertimeonline\.com|onehcm\.com|yourpayrollhr\.com|appone\.com|"
    r"tedk12\.com|schoolspring\.com|myjobs\.adp\.com|recruiting\.adp\.com|teamtailor|trinethire\.com|rippling\.com|feed=job_feed|"
    r"prismhr-hire\.com|hiringthing\.com|rippling-ats\.com|catsone\.com|rec\.pro\.ukg\.net|oraclegovcloud|"
    r"ashbyhq\.com|exacthire\.com|j\.brt\.mv|ejob\.bz|brightmove\.com|avature\.net|jobs\.keldair\.com|citysuitehr\.com|"
    r"connect-ats\.com|keldairaps\.com|constructhire\.com|strongpayhire\.com|go\.jobflows\.co|"
    r"hire\.trakstar\.com|recruiterbox\.com|eightfold\.ai|"
    r"atenterprise\.powerschool\.com|winocular\.com|willsubplus\.com|/ICS/[^\s\"']*\.jnz|focusschoolsoftware\.com|"
    r"inforcloudsuite\.com/hcm/Jobs|jobsyn\.org|[a-z0-9-]+\.(?:jobs|careers)/|"
    r"recruiting\.|jobs\.|careers\.|employment\.|hr\.", re.I)

# vendor marketing / app hosts that look like "<tenant>.<ats>.com" but are not a customer board
NON_TENANT_HOSTS = {"www", "apply", "jobs", "app", "api", "help", "support", "blog", "login", "admin",
                    "static", "cdn", "assets", "secure", "my", "go", "info", "about", "careers"}

_tls = threading.local()


def sess():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        s.max_redirects = 8
        _tls.s = s
    return _tls.s


def get(url):
    r = sess().get(url, timeout=TIMEOUT, allow_redirects=True)
    ctype = r.headers.get("content-type", "")
    text = r.text if ("html" in ctype or "xml" in ctype or not ctype) else ""
    return r.status_code, r.url, text


BLOCKED = re.compile(r"_Incapsula_Resource|incapsula|Just a moment|cf-browser-verification|challenge-platform|"
                     r"Access Denied|Request unsuccessful|Pardon Our Interruption|are you a human|captcha", re.I)


def blocked(code, html):
    """Bot wall or JS-only shell: a real browser may do better."""
    return code in (202, 403, 429, 503) or len(html or "") < 4000 or bool(BLOCKED.search((html or "")[:6000]))


def get_browser(url):
    """Same contract as get(), rendered in headless Chromium (one per thread).
    Gets through Imperva/Incapsula challenge pages and JS-rendered sites."""
    if not hasattr(_tls, "pw"):
        from playwright.sync_api import sync_playwright
        _tls.pw = sync_playwright().start()
        _tls.browser = _tls.pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        _tls.ctx = _tls.browser.new_context(user_agent=UA.replace("124.0.0.0", "130.0.0.0"), locale="en-US",
                                            viewport={"width": 1366, "height": 900})
    page = _tls.ctx.new_page()
    try:
        page.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|css)(\?|$)", re.I), lambda r: r.abort())
        resp = page.goto(url, wait_until="domcontentloaded", timeout=25000)
        code = resp.status if resp else 0
        page.wait_for_timeout(1500)
        html = page.content()
        if BLOCKED.search(html[:6000]) or code == 202:       # challenge: give it time to solve
            page.wait_for_timeout(6000)
            html = page.content()
        if BLOCKED.search(page.title() or ""):
            code = 403
        return code, page.url, html
    finally:
        page.close()


# --- detectors for ATSes the dashboard already fetches -----------------------
WD_RE = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")


def verify_workday(tenant, wd, site):
    try:
        r = requests.post(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                          json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                          headers={**HEADERS, "Accept": "application/json", "Content-Type": "application/json"},
                          timeout=15)
        if r.status_code == 200 and "jobPostings" in r.text:
            return int(r.json().get("total") or 0)
        return -1
    except Exception:
        return -1


WD_SITE_RE = re.compile(r"https?://[a-z0-9.-]*myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([a-z0-9-]+)/([A-Za-z0-9_-]+)")


def detect_workday(url, html):
    from collections import Counter
    cands = Counter()
    for m in WD_RE.finditer(url):
        cands[m.groups()] += 100
    for m in WD_RE.finditer(html or ""):
        cands[m.groups()] += 1
    # myworkdaysite.com/recruiting/<tenant>/<site> hides the wd shard; try the common ones
    for src, w in ((url, 100), (html or "", 1)):
        for m in WD_SITE_RE.finditer(src):
            for wd in ("wd1", "wd5", "wd3", "wd12", "wd10", "wd501", "wd503", "wd504", "wd108"):
                cands[(m.group(1), wd, m.group(2))] += w
    best, best_total = None, -1
    tried = 0
    for (tenant, wd, site), _ in cands.most_common():
        if site.lower() in ("wday", "login", "job", "en-us"):
            continue
        tried += 1
        if tried > 8:
            break
        total = verify_workday(tenant, wd, site)   # prefer the site with the most postings
        if total > best_total:
            best, best_total = (tenant, wd, site), total
    if best and best_total >= 0:
        tenant, wd, site = best
        return {"ats": "workday", "tenant": tenant, "wd": wd, "site": site,
                "url": f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"}
    return None


def detect_existing(url, html):
    """ATS the dashboard already fetches. Checked on the page URL first, then on
    every ATS-looking link in the page (an HR page usually links the board)."""
    cfg = detect_workday(url, html)
    if cfg:
        return cfg
    cfg = _detect_existing_url(url)
    if cfg:
        return cfg
    for m in re.finditer(r"""(?:href|src)=["']([^"']+)""", html or "", re.I):
        u = m.group(1).replace("&amp;", "&")
        if ATS_HOST.search(u):
            cfg = _detect_existing_url(urljoin(url, u))
            if cfg:
                return cfg
    # ADP WorkforceNow embedded only as a web component, no recruitment.html link
    m = re.search(r"<recruitment-current-openings\b([^>]*)>", html or "", re.I)
    if m:
        attrs = dict(re.findall(r"""([a-zA-Z-]+)=["']([^"']*)["']""", m.group(1)))
        cid = attrs.get("cid", "")
        if re.fullmatch(r"[0-9a-f-]{36}", cid):
            cfg = {"ats": "adp", "cid": cid, "url": url}
            if attrs.get("ccid") and attrs["ccid"] != "19000101_000001":
                cfg["ccId"] = attrs["ccid"]
            return cfg
    # BambooHR embed script / iframe without an ATS-looking href
    m = re.search(r"(?:https?:)?//([a-z0-9-]+)\.bamboohr\.com/(?:js/embed\.js|jobs/embed|careers)", html or "", re.I)
    if m:
        return {"ats": "bamboohr", "slug_id": m.group(1).lower(), "url": url}
    return None


def _detect_existing_url(url):
    m = re.search(r"https?://([a-z0-9-]+)\.icims\.com", url)
    if m and not m.group(1).startswith("internal-"):
        return {"ats": "icims", "slug_id": m.group(1), "url": url}
    m = re.search(r"https?://([a-z0-9-]+)\.csod\.com/ux/ats/careersite/(\d+)", url)
    if m:
        return {"ats": "csod", "host": m.group(1), "site": m.group(2), "url": url}
    m = re.search(r"https?://([a-z0-9-]+)\.csod\.com/ats/careersite/search\.aspx\?.*?site=(\d+)", url, re.I)
    if m:   # classic careersite URL; the ux endpoint serves the same site id
        return {"ats": "csod", "host": m.group(1), "site": m.group(2),
                "url": f"https://{m.group(1)}.csod.com/ux/ats/careersite/{m.group(2)}/home"}
    m = re.search(r"recruiting\d*\.ultipro\.com/([A-Z0-9]+)/JobBoard/([0-9a-f-]{36})", url, re.I)
    if m:
        return {"ats": "ultipro", "tenant": m.group(1), "board": m.group(2), "url": url}
    m = re.search(r"https?://([a-z0-9-]+\.rec\.pro\.ukg\.net)/([A-Z0-9]+)/JobBoard/([0-9a-f-]{36})", url, re.I)
    if m:   # UKG Pro on the newer rec.pro.ukg.net hosts (same JobBoard API)
        return {"ats": "ultipro", "host": m.group(1).lower(), "tenant": m.group(2), "board": m.group(3), "url": url}
    m = re.search(r"recruiting\.paylocity\.com/recruiting/jobs/All/([0-9a-f-]{36})", url, re.I)
    if m:
        return {"ats": "paylocity", "guid": m.group(1), "url": url}
    m = re.search(r"(workforcenow(?:\.cloud)?\.adp\.com)/.*?cid=([0-9a-f-]{36})", url, re.I)
    if m:
        cfg = {"ats": "adp", "cid": m.group(2), "url": url}
        cc = re.search(r"ccId=([0-9_]+)", url, re.I)
        if cc and cc.group(1) != "19000101_000001":
            cfg["ccId"] = cc.group(1)
        if m.group(1).lower() != "workforcenow.adp.com":
            cfg["host_override"] = m.group(1).lower()
        return cfg
    m = re.search(r"https?://(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", url)
    if m and m.group(1).lower() not in ("api", "www"):
        return {"ats": "smartrecruiters", "slug_id": m.group(1), "url": url}
    m = re.search(r"paycomonline\.net/v4/ats/web\.php/(?:jobs\?clientkey=|portal/)([0-9A-F]{16,})", url, re.I)
    if m:
        return {"ats": "paycom", "clientkey": m.group(1), "url": url}
    m = re.search(r"https?://([a-z0-9-]+)\.applicantpro\.com", url)
    if m and m.group(1).lower() not in NON_TENANT_HOSTS:   # footer links to www.applicantpro.com are not a board
        return {"ats": "applicantpro", "slug_id": m.group(1), "url": url}
    m = re.search(r"apply\.workable\.com/([a-z0-9-]+)", url)
    if m:
        return {"ats": "workable", "slug_id": m.group(1), "url": url}
    m = re.search(r"boards\.greenhouse\.io/([a-z0-9-]+)|job-boards\.greenhouse\.io/([a-z0-9-]+)", url)
    if m:
        return {"ats": "greenhouse", "slug_id": m.group(1) or m.group(2), "url": url}
    m = re.search(r"jobs\.lever\.co/([a-z0-9-]+)", url)
    if m:
        return {"ats": "lever", "slug_id": m.group(1), "url": url}
    m = re.search(r"https?://([a-z0-9-]+)\.bamboohr\.com", url)
    if m:
        return {"ats": "bamboohr", "slug_id": m.group(1), "url": url}
    m = re.search(r"jobs\.jobvite\.com/([a-z0-9-]+)", url)
    if m:
        return {"ats": "jobvite", "slug_id": m.group(1), "url": url}
    m = re.search(r"https?://([a-z0-9-]+)\.dayforcehcm\.com/CandidatePortal/[a-z-]+/([a-zA-Z0-9]+)", url, re.I)
    if m:
        return {"ats": "dayforce", "client": m.group(1), "site": m.group(2), "url": url}
    m = re.search(r"https?://jobs\.dayforcehcm\.com/[a-z]{2}-[A-Z]{2}/([a-zA-Z0-9]+)/([a-zA-Z0-9]+)", url)
    if m:   # newer jobs.dayforcehcm.com/en-US/<client>/<site> style; same search API
        return {"ats": "dayforce", "client": m.group(1), "site": m.group(2), "url": url}
    return None


def classify(url, html):
    cfg = detect_existing(url, html) or detect_college(url, html)
    return cfg


def looks_like_listing(html):
    """Heuristic: page has several links whose text contains a job-ish title word."""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    n = 0
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if 8 < len(t) < 160 and re.search(r"\b(adjunct|instructor|professor|faculty|lecturer|coordinator|specialist|"
                                          r"assistant|director|manager|technician|analyst|advisor|counselor)\b", t, re.I):
            n += 1
            if n >= 2:
                return True
    return False


def candidate_links(base_url, html, limit=5):
    soup = BeautifulSoup(html, "html.parser")
    scored = {}
    for fr in soup.find_all("iframe", src=True):      # boards embedded in an HR page
        u = urljoin(base_url, fr["src"].strip())
        if u.startswith("http") and ATS_HOST.search(u):
            scored[u] = 9
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http"):
            continue
        text = a.get_text(" ", strip=True)[:120]
        blob = f"{text} {url}"
        s = 0
        if ATS_HOST.search(url):
            s += 5
        if STRONG.search(text):
            s += 3
        elif JOB_LINK.search(text):
            s += 1
        if STRONG.search(urlsplit(url).path + urlsplit(url).netloc):
            s += 2
        if re.search(r"student[- _](jobs|employment)|work[- ]study|career[- _](services|center|centre|development|"
                     r"job[- _]placement|outcomes|readiness|exploration|counseling|technical|pathways)|"
                     r"handshake|alumni|internship|job[- _](shadow|fair)|gainful[- _]employment|"
                     r"/academics?/|/programs?/|/admissions?/|financial[- _]aid|/cte\b", blob, re.I):
            s -= 6
        if re.search(r"human[- _]resources|/hr\b|/jobs?\b|/employment\b|/careers?\b|job[- _]openings|"
                     r"academic[- _](jobs|positions|employment)|faculty[- _](jobs|positions|openings)|"
                     r"work[- _](at|for|with)", url, re.I):
            s += 2
        if s >= 3:
            scored[url] = max(s, scored.get(url, 0))
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:limit]]


def guess_urls(homepage):
    host = urlsplit(homepage).netloc
    root = re.sub(r"^www\.", "", host)
    return [f"https://jobs.{root}", f"https://careers.{root}", f"https://employment.{root}",
            f"{homepage}/jobs", f"{homepage}/careers", f"{homepage}/employment", f"{homepage}/hr"]


RANK = {"ok": 2, "generic": 1, "none": 0, "error": 0}


def discover_one(inst, browser=False):
    """Crawl with requests; if that finds no classified board and `browser` is
    set, crawl again in headless Chromium (bot walls, JS-rendered HR pages)."""
    rec = _discover(inst, get)
    if browser and rec["status"] != "ok":
        try:
            rec2 = _discover(inst, get_browser, max_hops=10)
        except Exception as e:
            rec["error"] = f"browser: {type(e).__name__}"
            return rec
        rec2["candidates"] = list(dict.fromkeys(rec["candidates"] + rec2["candidates"]))
        if RANK[rec2["status"]] >= RANK[rec["status"]]:
            rec2["via"] = "browser"
            return rec2
    return rec


def _discover(inst, fetch, max_hops=14):
    rec = {**inst, "status": "none", "ats": None, "config": None, "configs": [], "candidates": [],
           "checked": time.strftime("%Y-%m-%d")}
    tried = set()
    queue = []
    try:
        code, final, html = fetch(inst["homepage"])
        if code < 400 and html:
            queue += candidate_links(final, html)
        if fetch is get and blocked(code, html):
            rec["blocked"] = True
    except Exception as e:
        rec["error"] = f"home: {type(e).__name__}"
    queue += guess_urls(inst["homepage"])
    generic_hit = None
    hops = 0
    found = []
    while queue and hops < max_hops and len(found) < 3:
        url = queue.pop(0)
        key = re.sub(r"[#?].*$", "", url).rstrip("/").lower()
        if key in tried:
            continue
        tried.add(key)
        hops += 1
        try:
            code, final, html = fetch(url)
        except Exception:
            continue
        if code >= 400 or not html:
            continue
        rec["candidates"].append(final)
        cfg = classify(final, html)
        if cfg:
            sig = json.dumps({k: v for k, v in cfg.items() if k != "url"}, sort_keys=True)
            if sig not in {json.dumps({k: v for k, v in c.items() if k != "url"}, sort_keys=True) for c in found}:
                found.append(cfg)
            continue
        for u in candidate_links(final, html, limit=4):
            if ATS_HOST.search(u) or STRONG.search(urlsplit(u).path):
                queue.insert(0, u)
        if looks_like_listing(html):
            studenty = re.search(r"career[- _](services|center|centre|job[- _]placement)|student", final, re.I)
            if generic_hit is None or (not studenty and re.search(r"career[- _](services|center|centre|job[- _]placement)|student", generic_hit, re.I)):
                generic_hit = final
    if found:
        rec.update(status="ok", ats=found[0]["ats"], config=found[0], configs=found)
    elif generic_hit:
        cfg = {"ats": "college_generic", "url": generic_hit}
        rec.update(status="generic", ats="college_generic", config=cfg, configs=[cfg])
    return rec


def _sig(cfg):
    return json.dumps({k: v for k, v in cfg.items() if k not in ("slug", "name", "location", "state")}, sort_keys=True)


def inherit_from_systems(reg, insts):
    """Campuses of one system (IPEDS F1SYSNAM) usually share one board. A board
    found at the system office (sector 0) or at 2+ sister campuses is given to
    the sisters where nothing better was found. Returns the number updated."""
    by_sys = {}
    for i in insts:
        if i.get("system"):
            by_sys.setdefault(i["system"], []).append(i["unitid"])
    n = 0
    for system, ids in by_sys.items():
        votes = {}
        for uid in ids:
            r = reg.get(uid)
            if not r or r.get("status") != "ok" or r.get("inherited"):
                continue
            for c in r.get("configs") or []:
                if c.get("ats") == "college_generic":
                    continue
                w = 2 if r.get("sector") == 0 else 1
                votes.setdefault(_sig(c), [0, c])[0] += w
        shared = [c for w, c in votes.values() if w >= 2]
        if not shared:
            continue
        for uid in ids:
            r = reg.get(uid)
            if not r or r.get("status") == "ok" or r.get("sector") == 0:
                continue
            have = {_sig(c) for c in r.get("configs") or []}
            new = [dict(c) for c in shared if _sig(c) not in have]
            if not new:
                continue
            r["configs"] = new + [c for c in r.get("configs") or []]
            r.update(status="ok", ats=new[0]["ats"], config=new[0], inherited=system)
            n += 1
    return n


def reclassify_one(rec):
    """Re-run the detectors on the pages a previous crawl already found (cheap)."""
    found = []
    for url in (rec.get("candidates") or [])[:5]:
        try:
            code, final, html = get(url)
        except Exception:
            continue
        if code >= 400 or not html:
            continue
        cfg = classify(final, html)
        if cfg and cfg not in found:
            found.append(cfg)
    if found:
        rec.update(status="ok", ats=found[0]["ats"], config=found[0], configs=found)
    rec["checked"] = time.strftime("%Y-%m-%d")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reclassify", action="store_true",
                    help="re-run detectors on already-visited pages of unclassified schools (fast)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true", help="re-crawl everything")
    ap.add_argument("--redo-unclassified", action="store_true", help="re-crawl only schools without a classified board")
    ap.add_argument("--redo-errors", action="store_true",
                    help="re-crawl only unclassified schools whose last crawl hit a fetch/browser error")
    ap.add_argument("--states", nargs="*")
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--sectors", nargs="*", type=int)
    ap.add_argument("--browser", action="store_true",
                    help="retry unclassified schools in headless Chromium (bot walls, JS-only sites)")
    ap.add_argument("--inherit-only", action="store_true",
                    help="just share boards across campuses of the same IPEDS system, then rebuild")
    args = ap.parse_args()

    reg = registry.load_registry()
    if args.build_only:
        p = registry.write_colleges_json(reg)
        print(f"wrote {p}")
        return
    if args.reclassify:
        recs = [r for r in reg.values() if r.get("status") != "ok" and r.get("candidates")]
        print(f"reclassifying {len(recs)} schools", flush=True)
        n = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rec in ex.map(reclassify_one, recs):
                reg[rec["unitid"]] = rec
                n += rec["status"] == "ok"
        registry.save_registry(reg)
        print(f"  {n} newly classified")
        print(f"wrote {registry.write_colleges_json(reg)}")
        return
    insts = institutions.load_all(sectors=args.sectors)
    for i in insts:                       # keep system membership current on old records
        if i["unitid"] in reg:
            reg[i["unitid"]]["system"] = i.get("system", "")
            reg[i["unitid"]]["sector"] = i["sector"]
    if args.inherit_only:
        n = inherit_from_systems(reg, insts)
        registry.save_registry(reg)
        print(f"  {n} campuses inherited a system board")
        print(f"wrote {registry.write_colleges_json(reg)}")
        return
    if args.states:
        st = {s.upper() for s in args.states}
        insts = [i for i in insts if i["state"] in st]
    if args.redo_errors:
        insts = [i for i in insts if reg.get(i["unitid"], {}).get("status") != "ok"
                 and reg.get(i["unitid"], {}).get("error")]
    elif args.redo_unclassified:
        insts = [i for i in insts if reg.get(i["unitid"], {}).get("status") != "ok"]
    elif not args.redo:
        insts = [i for i in insts if i["unitid"] not in reg or reg[i["unitid"]].get("status") == "error"]
    if args.limit:
        insts = insts[:args.limit]
    print(f"discovering {len(insts)} institutions ({args.workers} workers)", flush=True)
    t0 = time.time()
    done = 0
    stats = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(discover_one, i, args.browser): i for i in insts}
        for f in as_completed(futs):
            inst = futs[f]
            try:
                rec = f.result()
            except Exception as e:
                rec = {**inst, "status": "error", "error": repr(e)[:200]}
            reg[inst["unitid"]] = rec
            stats[rec.get("ats") or rec["status"]] = stats.get(rec.get("ats") or rec["status"], 0) + 1
            done += 1
            if done % 50 == 0 or done == len(insts):
                registry.save_registry(reg)
                print(f"  {done}/{len(insts)}  {time.time()-t0:.0f}s  {json.dumps(stats, sort_keys=True)}", flush=True)
    n = inherit_from_systems(reg, insts)
    print(f"  {n} campuses inherited a system board")
    registry.save_registry(reg)
    p = registry.write_colleges_json(reg)
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
