#!/usr/bin/env python3
"""Find job boards for schools the site crawl missed, from the vendor side.

    python3 -m colleges.vendor_sweep                        # all default angles
    python3 -m colleges.vendor_sweep --angles vendor ihe    # subset
    python3 -m colleges.vendor_sweep --angles hej --limit 40  # smoke test
    python3 -m colleges.vendor_sweep --angles bing --limit 200

Instead of crawling each school's website (colleges/discover.py), collect
(institution name, board URL) pairs from employer directories and vendor hosts,
map them to IPEDS institutions whose registry status is not "ok", verify every
board URL with the ATS detectors, and write data/vendor_candidates.json:

    {unitid: {"name", "state", "source", "configs": [cfg, ...]},
     "_unmatched": [[name, url, ats], ...]}        # boards nobody could be mapped to

Angles:
  vendor  probe vendor hosts derived from the school's domain / name
          (<slug>.peopleadmin.com, <slug>.interviewexchange.com, NeoGov
          schooljobs/governmentjobs agencies, hr.<domain>, <home>/postings/search)
  ihe     careers.insidehighered.com/employers/ directory -> employer page ->
          first job -> /apply/ page, whose meta-refresh is the school's own ATS
  herc    regional HERC member lists (<region>.hercjobs.org/website/<id>/member-institutions.cfm)
  hej     higheredjobs.com institution list (Playwright; the Incapsula wall is
          cleared by one in-page click) -> job detail -> /clickthru/ redirect
  bing    Bing search in Playwright for the "none" schools (opt-in, rate-limited)

The registry is read-only here (another process may be rewriting it); it is
reloaded at the end so schools that became "ok" meanwhile are dropped.
"""
import argparse
import base64
import difflib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs, quote

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from colleges import institutions, registry  # noqa: E402
from colleges.discover import classify, sess, HEADERS, UA, ATS_HOST, _sig  # noqa: E402

OUT = ROOT / "data" / "vendor_candidates.json"
TIMEOUT = 10
WORKERS = 16
_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


# --------------------------------------------------------------------------- fetch / verify
def get(url, timeout=TIMEOUT):
    r = sess().get(url, timeout=timeout, allow_redirects=True)
    ctype = r.headers.get("content-type", "")
    text = r.text if ("html" in ctype or "xml" in ctype or not ctype) else ""
    return r.status_code, r.url, text


def origin(url):
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def verify(url, html=None):
    """Run the ATS detectors on a board URL. Returns a config dict or None.
    A job-detail URL is fine: the detectors read the host/path, and the board
    origin is tried too (PeopleAdmin and friends)."""
    if not url or not url.startswith("http"):
        return None
    if re.search(r"insidehighered|chronicle\.com|higheredjobs|hercjobs|academicjobsonline|indeed\.com|linkedin\.com|"
                 r"glassdoor|ziprecruiter|simplyhired", url, re.I):
        return None
    try:
        if html is None:
            code, final, html = get(url)
            if code >= 400:
                return None
        else:
            final = url
        cfg = classify(final, html)
        if cfg:
            return cfg
        o = origin(final)
        if o.rstrip("/") != final.rstrip("/"):
            code, final2, html2 = get(o)
            if code < 400 and html2:
                return classify(final2, html2)
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- institutions + matching
STOP = re.compile(r"\b(the|of|at|and|inc|llc)\b")
MAIN = re.compile(r"\s*[-–:]\s*main campus$|\s*\(main campus\)$", re.I)
CAMPUS_WORD = re.compile(r"\s+campus$", re.I)
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR"}


def state_hint(name):
    """'Mount St. Mary's University (Maryland)' -> 'MD'; 'X College of Missouri' -> 'MO'."""
    m = re.search(r",\s*([A-Z]{2})\s*$", name or "")                 # "Bethany College, KS"
    if m and m.group(1) in US_STATES.values():
        return m.group(1)
    m = re.search(r"\(([A-Za-z ]+)\)\s*$", name or "") or re.search(r"\bof ([A-Za-z ]+)$", name or "")
    if m:
        return US_STATES.get(m.group(1).strip().lower())
    return None


def norm(name):
    s = (name or "").lower().replace("&", " and ").replace("'", "")
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)                  # "(Maryland)"
    s = re.sub(r",\s*[a-z]{2}\s*$", "", s)                     # ", ks"
    s = MAIN.sub("", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bagri\.", "agricultural", s)
    s = re.sub(r"\bcomm\.?\b", "community", s)
    s = re.sub(r"\buniv\.?\b", "university", s)
    s = re.sub(r"\bcoll\.?\b", "college", s)
    s = re.sub(r"\btech\.?\b", "technical", s)
    s = re.sub(r"\bstate university of new york\b( college)?( at)?", "suny", s)
    s = re.sub(r"\bcity university of new york\b", "cuny", s)
    s = re.sub(r"\bsuny college at\b", "suny", s)
    s = CAMPUS_WORD.sub("", s)                                # "-Bothell Campus" -> "-bothell"
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = STOP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def apex(host):
    host = re.sub(r"^www\.", "", (host or "").lower())
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("edu", "ac", "co", "org", "com") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])          # x.edu.xx style
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class Index:
    """Every IPEDS institution (not just targets) so a board for an already-ok
    school is not mis-attributed to a near-namesake that still needs one."""

    def __init__(self, insts, reg):
        self.insts = {i["unitid"]: i for i in insts}
        self.reg = reg
        self.size = {}
        try:
            import csv
            with open(institutions.CSV_PATH, newline="") as f:
                for row in csv.DictReader(f):
                    self.size[row["UNITID"]] = int(row.get("INSTSIZE") or -1)
        except Exception:
            pass
        self.by_name = {}
        self.by_domain = {}
        for i in insts:
            self.by_name.setdefault(norm(i["name"]), []).append(i["unitid"])
            self.by_domain.setdefault(apex(urlsplit(i["homepage"]).netloc), []).append(i["unitid"])
        self.names = list(self.by_name)

    def status(self, uid):
        return self.reg.get(uid, {}).get("status", "missing")

    def is_target(self, uid):
        return self.status(uid) != "ok"

    def targets(self):
        return [i for uid, i in self.insts.items() if self.is_target(uid)]

    def _pick(self, uids, state=None):
        if state:
            st = [u for u in uids if self.insts[u]["state"] == state.upper()]
            if st:
                uids = st
        if len(uids) == 1:
            return uids[0]
        if not uids:
            return None
        # several campuses: prefer the one whose IPEDS name has no campus suffix,
        # else the biggest campus (IPEDS INSTSIZE) when that is unique
        plain = [u for u in uids if not re.search(r"[-–]", self.insts[u]["name"])]
        if len(plain) == 1:
            return plain[0]
        sizes = sorted(((self.size.get(u, -1), u) for u in uids), reverse=True)
        if sizes[0][0] > 0 and (len(sizes) == 1 or sizes[0][0] > sizes[1][0]):
            return sizes[0][1]
        return None

    def match_name(self, name, state=None):
        n = norm(name)
        if not n:
            return None
        state = state or state_hint(name)
        if n in self.by_name:
            return self._pick(self.by_name[n], state)
        # "X University" vs "X University-Main Campus" style: unique prefix match
        fwd = [k for k in self.names if k.startswith(n + " ")]           # "x university" -> "x university-campus"
        if 1 <= len(fwd) <= 12:
            uid = self._pick([u for k in fwd for u in self.by_name[k]], state)   # campuses: biggest wins
            if uid:
                return uid
        rev = [k for k in self.names if n.startswith(k + " ") and len(n[len(k):].split()) <= 2]
        if len(rev) == 1:                                                  # "x university main" -> "x university"
            return self._pick(self.by_name[rev[0]], state)
        for cand in difflib.get_close_matches(n, self.names, n=3, cutoff=0.92):
            uid = self._pick(self.by_name[cand], state)
            if uid:
                return uid
        # "Alvin College" vs "Alvin Community College": every token present, unique, with a distinctive one
        toks = set(n.split())
        if distinctive(name):
            sub = [k for k in self.names if toks <= set(k.split())]
            if len(sub) == 1:
                return self._pick(self.by_name[sub[0]], state)
        if state:
            for cand in difflib.get_close_matches(n, self.names, n=3, cutoff=0.85):
                uid = self._pick(self.by_name[cand], state)
                if uid and self.insts[uid]["state"] == state.upper():
                    return uid
        return None

    def match_domain(self, url, state=None):
        a = apex(urlsplit(url).netloc)
        if a in self.by_domain:
            return self._pick(self.by_domain[a], state)
        return None


# --------------------------------------------------------------------------- results
class Results:
    def __init__(self, index):
        self.index = index
        self.found = {}         # unitid -> {"name", "state", "source", "configs"}
        self.unmatched = []     # [name, url, ats]
        self.lock = threading.Lock()
        self.per_angle = {}

    def add(self, uid, cfg, angle):
        if not cfg or not uid:
            return False
        with self.lock:
            inst = self.index.insts[uid]
            rec = self.found.setdefault(uid, {"name": inst["name"], "state": inst["state"], "source": angle, "configs": []})
            if angle not in rec["source"].split("+"):
                rec["source"] += "+" + angle
            if _sig(cfg) in {_sig(c) for c in rec["configs"]}:
                return False
            rec["configs"].append(cfg)
            self.per_angle.setdefault(angle, set()).add(uid)
            return True

    def add_unmatched(self, name, cfg, angle):
        with self.lock:
            if not any(u[1] == cfg["url"] for u in self.unmatched):
                self.unmatched.append([name, cfg["url"], cfg["ats"], angle])

    def load(self, path=OUT):
        if path.exists():
            d = json.loads(path.read_text())
            self.unmatched = d.pop("_unmatched", [])
            self.found = {k: v for k, v in d.items() if not k.startswith("_")}

    def save(self, path=None):
        path = path or getattr(self, "out", OUT)
        d = dict(sorted(self.found.items()))
        d["_unmatched"] = sorted(self.unmatched)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.replace(path)
        return path


# --------------------------------------------------------------------------- angle: vendor host probes
def slugs_for(inst):
    host = re.sub(r"^www\d*\.", "", urlsplit(inst["homepage"]).netloc.lower())
    parts = host.split(".")
    out = []
    if parts and parts[0] not in ("edu", "org", "com"):
        out.append(parts[0])
    a = apex(host).split(".")[0]
    if a not in out:
        out.append(a)
    n = norm(inst["name"])
    compact = re.sub(r"\s+", "", n)
    if compact and compact not in out and len(compact) <= 40:
        out.append(compact)
    words = n.split()
    if len(words) >= 2:
        acr = "".join(w[0] for w in words if w not in ("community", "college", "university", "state", "technical"))
        if 2 <= len(acr) <= 6 and acr not in out:
            out.append(acr)
    return [s for s in dict.fromkeys(out) if re.match(r"^[a-z0-9-]{2,}$", s)]


GENERIC_TOKENS = {"university", "college", "community", "state", "technical", "institute", "school", "schools",
                  "center", "campus", "district", "system", "main", "city", "county", "junior", "technology",
                  "arts", "sciences", "science", "medical", "health", "christian", "international", "saint", "st",
                  "north", "south", "east", "west", "northern", "southern", "eastern", "western", "central"}


def distinctive(name):
    return [t for t in norm(name).split() if t not in GENERIC_TOKENS and len(t) > 2]


def names_page(name, text):
    """A distinctive token of the school's name (or the whole name) occurs in `text`."""
    t = norm(text)
    n = norm(name)
    if n and n in t:
        return True
    toks = distinctive(name)
    return bool(toks) and any(re.search(rf"\b{re.escape(tok)}\b", t) for tok in toks)


def _neogov_live(domain, agency, inst):
    """NeoGov answers 200 with the same shell for any agency, and a slug such as
    'berkeley' may be a city or a K-12 district: the careers page must name
    the school (title, logo alt text or the welcome blurb)."""
    try:
        code, final, html = get(f"https://www.{domain}.com/careers/{agency}")
        if code != 200 or not html:
            return False
        soup = BeautifulSoup(html, "html.parser")
        bits = [soup.title.get_text(" ", strip=True) if soup.title else ""]
        bits += [img.get("alt", "") for img in soup.find_all("img", alt=True)]
        bits += [d.get_text(" ", strip=True)[:600] for d in soup.find_all("div", class_=re.compile("verbiage|agency-?name|header", re.I))]
        bits += [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])]
        text = " ".join(bits)
        if text.startswith("GovernmentJobs |") or text.startswith("SchoolJobs |"):
            return False
        return re.search(r"\b(college|university|institute|school of|seminary|conservatory)\b", text, re.I) \
            and names_page(inst["name"], text)
    except Exception:
        return False


def _ie_live(url):
    """InterviewExchange returns a page for unknown clients too; require a real client page."""
    try:
        code, final, html = get(url)
        return code == 200 and "interviewexchange" in final and re.search(r"jobofferslist|/static/clients/\d+", html or "")
    except Exception:
        return False


def probe_vendor(inst, res):
    home = inst["homepage"].rstrip("/")
    root = re.sub(r"^www\d*\.", "", urlsplit(home).netloc.lower())
    hits = 0
    for slug in slugs_for(inst):
        if _neogov_live("schooljobs", slug, inst):
            hits += res.add(inst["unitid"], {"ats": "college_neogov", "agency": slug,
                                             "url": f"https://www.schooljobs.com/careers/{slug}"}, "vendor")
        elif _neogov_live("governmentjobs", slug, inst):
            hits += res.add(inst["unitid"], {"ats": "college_neogov", "agency": slug,
                                             "url": f"https://www.governmentjobs.com/careers/{slug}"}, "vendor")
        cfg = verify(f"https://{slug}.peopleadmin.com")
        if cfg:
            hits += res.add(inst["unitid"], cfg, "vendor")
        ie = f"https://{slug}.interviewexchange.com/jobofferslist.jsp"
        if _ie_live(ie):
            cfg = verify(ie)
            if cfg:
                hits += res.add(inst["unitid"], cfg, "vendor")
    for u in (f"https://hr.{root}", f"{home}/postings/search", f"https://jobs.{root}/postings/search",
              f"https://employment.{root}/postings/search"):
        cfg = verify(u)
        if cfg:
            hits += res.add(inst["unitid"], cfg, "vendor")
    return hits


def angle_vendor(index, res, limit=None, workers=WORKERS):
    targets = index.targets()
    if limit:
        targets = targets[:limit]
    log(f"[vendor] probing {len(targets)} schools")
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe_vendor, i, res): i for i in targets}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                log("  err", futs[f]["name"], repr(e)[:80])
            done += 1
            if done % 100 == 0:
                log(f"  [vendor] {done}/{len(targets)} {time.time()-t0:.0f}s found={len(res.per_angle.get('vendor', ()))}")
                res.save()
    log(f"[vendor] done: {len(res.per_angle.get('vendor', ()))} schools, {time.time()-t0:.0f}s")


# --------------------------------------------------------------------------- angle: Inside Higher Ed employers
IHE = "https://careers.insidehighered.com"


def ihe_directory():
    """[(name, employer_url)] from the paginated /employers/ listing."""
    out = []
    page = 1
    while page < 400:
        html = ""
        for attempt in range(3):                     # the site is slow; a timeout must not end the walk
            try:
                code, final, html = get(f"{IHE}/employers/{page}" if page > 1 else f"{IHE}/employers/", timeout=25)
                if code < 400 and html:
                    break
            except Exception:
                html = ""
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        rows = [(a.get_text(" ", strip=True), urljoin(IHE, a["href"]))
                for a in soup.find_all("a", href=True) if a["href"].startswith("/employer/")]
        if not rows:
            break
        out += rows
        if not soup.find("a", href=f"/employers/{page+1}"):
            break
        page += 1
    return out


def ihe_board(employer_url):
    """First job's apply page carries a meta refresh to the real ATS."""
    code, final, html = get(employer_url)
    if code >= 400 or not html:
        return None
    m = re.search(r'href="(/job/\d+/[^"]*)"', html)
    if not m:
        return None
    jid = re.search(r"/job/(\d+)/", m.group(1)).group(1)
    code, final, html = get(f"{IHE}/apply/{jid}/x")
    if code >= 400:
        return None
    m = re.search(r'http-equiv="refresh"[^>]*url=([^"\']+)', html, re.I)
    return m.group(1).replace("&amp;", "&") if m else None


def ihe_one(name, url, index, res):
    uid = index.match_name(name)
    if uid and not index.is_target(uid):
        return
    board = ihe_board(url)
    if not board:
        return
    cfg = verify(board)
    if not cfg:
        return
    if uid is None:                         # second chance: the ATS host names the school
        uid = index.match_domain(board)
        if uid and not index.is_target(uid):
            return
    if uid:
        res.add(uid, cfg, "ihe")
    else:
        res.add_unmatched(name, cfg, "ihe")


def angle_ihe(index, res, limit=None, workers=WORKERS):
    rows = ihe_directory()
    log(f"[ihe] {len(rows)} employers in directory")
    if limit:
        rows = rows[:limit]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda r: _safe(ihe_one, r[0], r[1], index, res), rows))
    log(f"[ihe] done: {len(res.per_angle.get('ihe', ()))} schools, {len(res.unmatched)} unmatched total, {time.time()-t0:.0f}s")


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log("  err", fn.__name__, repr(e)[:100])


# --------------------------------------------------------------------------- angle: HERC regional member lists
def herc_member_pages():
    code, final, html = get("https://www.hercjobs.org/")
    regions = sorted(set(re.findall(r"https://www\.hercjobs\.org/regions/[a-z0-9-]+/", html or "")))
    pages = set()
    for reg in regions:
        try:
            code, final, html = get(reg)
        except Exception:
            continue
        for sub in set(re.findall(r"https://([a-z]+)\.hercjobs\.org", html or "")):
            if sub not in ("www", "main", "member"):
                pages.add(f"https://{sub}.hercjobs.org/website/698939/member-institutions.cfm")
        pages.update(re.findall(r"https://[a-z]+\.hercjobs\.org/website/\d+/[a-z-]*member[a-z-]*\.cfm", html or ""))
    return sorted(pages)


def angle_herc(index, res, limit=None, workers=WORKERS):
    pages = herc_member_pages()
    log(f"[herc] {len(pages)} regional member pages")
    pairs = []
    for p in pages:
        try:
            code, final, html = get(p)
        except Exception:
            continue
        if code >= 400 or not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            u = a["href"].strip()
            if not u.startswith("http") or "hercjobs" in u or re.search(r"facebook|twitter|linkedin|instagram|youtube|careereco|enable-javascript", u):
                continue
            path = urlsplit(u).path.strip("/")
            host = urlsplit(u).netloc.lower()
            if path or re.match(r"^(jobs|careers|employment|hr|recruit)", host) or ATS_HOST.search(u):
                pairs.append((a.get_text(" ", strip=True), u))
    pairs = list(dict.fromkeys(pairs))
    log(f"[herc] {len(pairs)} non-homepage member links")
    if limit:
        pairs = pairs[:limit]

    def one(name, u):
        uid = index.match_domain(u) or (index.match_name(name) if name and name.lower() != "home page" else None)
        if uid and not index.is_target(uid):
            return
        cfg = verify(u)
        if not cfg:
            return
        if uid:
            res.add(uid, cfg, "herc")
        else:
            res.add_unmatched(name or urlsplit(u).netloc, cfg, "herc")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda r: _safe(one, r[0], r[1]), pairs))
    log(f"[herc] done: {len(res.per_angle.get('herc', ()))} schools")


# --------------------------------------------------------------------------- angle: HigherEdJobs (Playwright)
HEJ = "https://www.higheredjobs.com"
_tls = threading.local()


def _browser_page():
    if not hasattr(_tls, "pw"):
        from playwright.sync_api import sync_playwright
        _tls.pw = sync_playwright().start()
        _tls.browser = _tls.pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        _tls.ctx = _tls.browser.new_context(user_agent=UA.replace("124.0.0.0", "130.0.0.0"), locale="en-US",
                                            viewport={"width": 1366, "height": 900})
        _tls.page = _tls.ctx.new_page()
        _tls.page.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|css)(\?|$)", re.I), lambda r: r.abort())
    return _tls.page


def _browser_close():
    if hasattr(_tls, "pw"):
        try:
            _tls.browser.close()
            _tls.pw.stop()
        except Exception:
            pass
        del _tls.pw


def hej_directory():
    """[(name, search_url)] from /institution/ (reachable in Chromium)."""
    page = _browser_page()
    for attempt in range(4):
        page.goto(f"{HEJ}/institution/", wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(5000 + 4000 * attempt)
        soup = BeautifulSoup(page.content(), "html.parser")
        rows = [(a.get_text(" ", strip=True), urljoin(HEJ + "/institution/", a["href"]))
                for a in soup.find_all("a", href=True) if "search.cfm?University=" in a["href"]]
        if len(rows) > 100:
            return rows
        log(f"  [hej] institution list not rendered (attempt {attempt+1}, {len(page.content())} bytes)")
    return []


def _hej_unlock(page):
    """The first search.cfm navigation is walled; an in-page click sets the
    Incapsula session cookie, after which direct navigation works."""
    if getattr(_tls, "hej_unlocked", False):
        return
    page.goto(f"{HEJ}/institution/", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(3000)
    try:
        page.click('a[href*="search.cfm?University="]', timeout=10000)
        page.wait_for_timeout(6000)
    except Exception:
        pass
    _tls.hej_unlocked = True


def _hej_walled(html):
    return len(html) < 3000 or "Request unsuccessful" in html or "_Incapsula_Resource" in html[:5000]


def hej_board(search_url):
    page = _browser_page()
    _hej_unlock(page)
    page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(2500)
    html = page.content()
    if _hej_walled(html):
        page.wait_for_timeout(6000)
        html = page.content()
        if _hej_walled(html):
            _tls.hej_unlocked = False
            return "walled"
    m = re.search(r'href="([^"]*details\.cfm\?JobCode=(\d+)[^"]*)"', html)
    if not m:
        return None
    page.goto(urljoin(page.url, m.group(1).replace("&amp;", "&")), wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(2500)
    html = page.content()
    m = re.search(r'href="(/clickthru/redirect\.cfm\?JobCode=\d+[^"]*)"', html)
    if not m:
        return None
    try:
        page.goto(urljoin(HEJ, m.group(1).replace("&amp;", "&")), wait_until="commit", timeout=25000)
        page.wait_for_timeout(2000)
    except Exception:
        pass
    final = page.url
    if "higheredjobs.com" in final:
        return None
    try:
        return final, page.content()
    except Exception:
        return final, ""


def hej_one(name, url, index, res):
    uid = index.match_name(name)
    if uid and not index.is_target(uid):
        return
    board = hej_board(url)
    if board == "walled":
        log(f"  [hej] walled on {name}")
        return
    if not board:
        return
    final, html = board
    cfg = verify(final, html) or verify(final)
    if not cfg:
        return
    if uid is None:
        uid = index.match_domain(final)
        if uid and not index.is_target(uid):
            return
    if uid:
        res.add(uid, cfg, "hej")
    else:
        res.add_unmatched(name, cfg, "hej")


def angle_hej(index, res, limit=None, workers=4):
    rows = hej_directory()
    _browser_close()
    log(f"[hej] {len(rows)} institutions listed")
    todo = []
    for name, url in rows:
        uid = index.match_name(name)
        if uid is None or index.is_target(uid):
            todo.append((name, url))
    log(f"[hej] {len(todo)} are targets or unmapped")
    if limit:
        todo = todo[:limit]
    t0 = time.time()
    done = 0

    def one(r):
        _safe(hej_one, r[0], r[1], index, res)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(one, todo):
            done += 1
            if done % 50 == 0:
                log(f"  [hej] {done}/{len(todo)} {time.time()-t0:.0f}s found={len(res.per_angle.get('hej', ()))}")
                res.save()
        # a browser must be closed by the thread that opened it: one close per worker
        barrier = threading.Barrier(workers, timeout=60)

        def close_mine(_):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            _browser_close()
        list(ex.map(close_mine, range(workers)))
    log(f"[hej] done: {len(res.per_angle.get('hej', ()))} schools, {time.time()-t0:.0f}s")


# --------------------------------------------------------------------------- angle: Bing (Playwright)
def _bing_links(page, q):
    page.goto(f"https://www.bing.com/search?q={quote(q)}", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)
    html = page.content()
    if re.search(r"captcha|unusual traffic|verify you are", html[:20000], re.I):
        return None
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("li.b_algo h2 a"):
        u = a.get("href", "")
        m = re.search(r"[?&]u=a1([A-Za-z0-9_-]+)", u)
        if m:
            try:
                u = base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)).decode()
            except Exception:
                continue
        if u.startswith("http"):
            out.append(u)
    return out


def angle_bing(index, res, limit=None, workers=1):
    targets = [i for i in index.targets() if index.status(i["unitid"]) == "none"]
    if limit:
        targets = targets[:limit]
    log(f"[bing] {len(targets)} 'none' schools")
    page = _browser_page()
    empty = 0
    for n, inst in enumerate(targets, 1):
        a = apex(urlsplit(inst["homepage"]).netloc)
        links = _bing_links(page, f'"{inst["name"]}" {inst["state"]} employment opportunities jobs')
        if links is None:
            log("[bing] challenge page: stopping")
            break
        if not links:
            empty += 1
            if empty >= 5:
                log("[bing] 5 empty result pages in a row: stopping (rate limit?)")
                break
        else:
            empty = 0
        for u in links[:6]:
            if apex(urlsplit(u).netloc) == a or ATS_HOST.search(u):
                cfg = verify(u)
                if cfg:
                    res.add(inst["unitid"], cfg, "bing")
                    break
        time.sleep(2)
        if n % 25 == 0:
            log(f"  [bing] {n}/{len(targets)} found={len(res.per_angle.get('bing', ()))}")
            res.save()
    _browser_close()
    log(f"[bing] done: {len(res.per_angle.get('bing', ()))} schools")


ANGLES = {"vendor": angle_vendor, "ihe": angle_ihe, "herc": angle_herc, "hej": angle_hej, "bing": angle_bing}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--angles", nargs="*", default=["vendor", "ihe", "herc", "hej"], choices=list(ANGLES))
    ap.add_argument("--limit", type=int, help="per-angle cap (smoke tests)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--fresh", action="store_true", help="ignore a previous vendor_candidates.json")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    reg = registry.load_registry()
    insts = institutions.load_all()
    index = Index(insts, reg)
    res = Results(index)
    res.out = args.out
    if not args.fresh:
        res.load(args.out)
    log(f"{len(insts)} institutions, {len(index.targets())} without a classified board "
        f"(registry: {len(reg)} records)")
    for a in args.angles:
        kw = {"limit": args.limit}
        if a in ("vendor", "ihe", "herc"):
            kw["workers"] = args.workers
        try:
            ANGLES[a](index, res, **kw)
        except Exception as e:
            log(f"[{a}] failed: {e!r}")
        res.save(args.out)

    # the crawl may have classified some of these meanwhile
    reg = registry.load_registry()
    now_ok = [u for u in res.found if reg.get(u, {}).get("status") == "ok"]
    for u in now_ok:
        del res.found[u]
    p = res.save(args.out)
    per = {a: len(s) for a, s in res.per_angle.items()}
    log(f"wrote {p}: {len(res.found)} schools with a verified board, {len(res.unmatched)} unmatched boards; "
        f"this run per angle: {json.dumps(per)}; dropped {len(now_ok)} now ok in registry")


if __name__ == "__main__":
    main()
