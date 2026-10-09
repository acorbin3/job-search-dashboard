#!/usr/bin/env python3
"""
Healthtech Director job scanner.

Pulls jobs from Greenhouse / Lever / Ashby public APIs for a curated list of
digital mental health + healthtech companies. Filters for Director-level
roles whose descriptions signal active AI investment.

Usage:
    python3 scrape.py
    python3 scrape.py --json results.json
    python3 scrape.py --min-ai-hits 3   (require 3+ AI keyword matches)
"""

import argparse
import html
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
import requests
from company_meta import get_meta
import fit_profiles

ROOT = Path(__file__).parent
COMPANIES_FILE = ROOT / "data" / "companies.json"

ETL_KEYWORDS = [
    "etl", "elt", "data pipeline", "data integration", "data replication",
    "data movement", "ingest", "ingestion", "cdc", "change data capture",
    "streaming", "kafka", "pulsar", "warehouse", "data lake", "lakehouse",
    "reverse etl", "data activation", "data sync", "iceberg", "delta lake",
    "snowflake", "databricks", "bigquery", "redshift", "dbt", "airflow", "dagster",
    "connector", "data infrastructure", "data platform", "data engineering",
    "orchestration", "spark", "flink", "data quality", "data observability",
    "data governance", "data catalog", "data mesh", "metadata",
]

# Role profiles ("tabs") live in profiles.json, managed from the UI at /tabs.
# Each entry: level / domain / exclude regex lists plus optional flags (see
# CLAUDE.md). Starter templates for new installs are in profile_templates.json.
PROFILES_FILE = ROOT / "profiles.json"


def load_profiles():
    """Return the tab config from profiles.json ({} when there is none yet)."""
    profiles = {}
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text())
            if isinstance(data, dict):
                for key, cfg in data.items():
                    if isinstance(cfg, dict) and cfg.get("level") and cfg.get("domain"):
                        profiles[key] = migrate_profile(cfg)
        except (json.JSONDecodeError, OSError):
            pass
    return profiles


def migrate_profile(cfg):
    """Rename legacy keys in place so older profiles.json files keep working."""
    # require_remote_or_tampa predates the configurable local area.
    if "require_remote_or_tampa" in cfg:
        cfg["require_remote_or_local"] = cfg.pop("require_remote_or_tampa")
        cfg.setdefault("local_re", "tampa")
    return cfg


PROFILES = load_profiles()


def profile_source(cfg):
    """Where a tab's rows come from: "ats" (the shared scan) or "linkedin".

    `source` is the switch; the older `external: true` flag is still honored so
    profiles.json files written before the field existed keep working.
    """
    src = (cfg or {}).get("source")
    if src in ("ats", "linkedin"):
        return src
    return "linkedin" if (cfg or {}).get("external") else "ats"

# Active filter globals; main()/set_superset_globals() point these at a profile.
_first = next(iter(PROFILES.values()), {"level": [], "domain": [], "exclude": []})
LEVEL_PATTERNS = _first["level"]
DOMAIN_PATTERNS = _first["domain"]
EXCLUDE_TITLE_PATTERNS = _first.get("exclude", [])
REQUIRE_ETL_SIGNAL = False
REQUIRE_HEALTHTECH_SECTOR = False
REQUIRE_AEROSPACE_SECTOR = False
REQUIRE_REMOTE_OR_LOCAL = False
LOCAL_RE = None          # compiled regex over the location string, or None
# When False (profile min_ai_hits == 0), fetchers keep title-matched roles even
# with no AI keyword in the description — used by the aerospace profile.
REQUIRE_AI_HITS = True

AI_KEYWORDS = [
    "ai strategy", "ai roadmap", "ai vision", "ai initiatives",
    "ai investment", "ai-powered", "ai powered", "ai-first", "ai first",
    "build ai", "building ai", "scale ai", "scaling ai",
    "machine learning platform", "ml platform",
    "generative ai", "genai", "gen ai",
    "large language model", "llm", "llms",
    "agentic", "agents",
    "ai/ml", "ai and ml", "ai & ml",
    "ai tooling", "ai-powered tooling",
    "ai adoption", "ai-driven", "ai driven",
    "ai capabilities", "ai-enabled", "ai enabled",
    "applied ai", "applied ml",
    "foundation model", "rag ", "retrieval augmented",
    "ai team", "ai org", "ai function",
]

UA = {
    "User-Agent": "healthtech-jobs-scanner/1.0 (personal research; respects robots)"
}

@dataclass
class Job:
    company: str
    ats: str
    title: str
    location: str
    url: str
    ai_hits: list
    ai_hit_count: int
    snippet: str
    description: str = ""   # full stripped description text
    posted_at: str = ""     # ISO 8601 of original posting if available
    updated_at: str = ""    # ISO 8601 of last update
    pay_min: int = 0        # USD annual, 0 if unknown
    pay_max: int = 0
    pay_text: str = ""      # raw display string
    remote: str = ""        # "remote" | "hybrid" | "onsite" | ""
    stage: str = ""         # "B" | "C" | "D" | "E+" | "Public" | ""
    sector: str = ""        # "healthtech" | "ai-native" | "data-infra" | "devtools" | "fintech" | "vertical-ai" | "other"
    fit: int = 0            # 0-100 resume-fit score (see compute_fit)
    # Healthcare signal, independent of the company's sector tag:
    #   "core" — the company itself is healthtech/medtech (company_meta)
    #   "adj"  — any other company, but the posting talks about healthcare
    #            (>= HEALTH_ADJ_MIN distinct terms from HEALTH_TERMS)
    #   ""     — no signal
    health: str = ""
    health_hits: list = field(default_factory=list)

# ---------------------------------------------------------------------------
# Resume-fit scoring: weighted keyword model, capped per dimension, capped at
# 100. The tables live in fit_profiles.json (see fit_profiles.py — one entry
# per resume, created from the UI by uploading a resume); select with --fit.
# Each profile: level (regex, points — first match wins, scanned over title),
# domains (regex, points-per-hit, cap — scanned over title+description),
# sector (slug-sector -> points), remote_bonus, location_re/location_bonus.
# Fit is recomputed on --from-raw, so switching --fit needs no network.

FIT_PROFILES = fit_profiles.load()
DEFAULT_FIT = fit_profiles.default_key()
ACTIVE_FIT = FIT_PROFILES[DEFAULT_FIT]

# ---------------------------------------------------------------------------
# Healthcare-adjacent signal. Company sector (company_meta) only tags companies
# whose product IS healthcare; plenty of roles at insurers, consultancies,
# fintechs, and generalist SaaS shops are still healthcare-facing. This scans
# the posting itself so those surface as "adj" — see Job.health.
HEALTH_TERMS = [
    (re.compile(r"\bclinical\b", re.I), "clinical"),
    (re.compile(r"\bpatients?\b", re.I), "patient"),
    (re.compile(r"\b(ehr|emr)s?\b|electronic (health|medical) records?", re.I), "EHR"),
    (re.compile(r"\bepic\b|\bcerner\b|\bathena(health)?\b|\bmeditech\b", re.I), "EHR vendor"),
    (re.compile(r"\bhipaa\b|\bphi\b|protected health information", re.I), "HIPAA/PHI"),
    (re.compile(r"\bfhir\b|\bhl7\b|\bccda?\b|\bx12\b|\b837\b|\b835\b", re.I), "FHIR/HL7"),
    (re.compile(r"health ?care|health-care", re.I), "healthcare"),
    (re.compile(r"health (system|plan|insur|network)s?|\bpayers?\b|\bpayors?\b", re.I), "payer/health system"),
    (re.compile(r"\bhospitals?\b|\bclinics?\b|\bhealth centers?\b", re.I), "hospital/clinic"),
    (re.compile(r"\bphysicians?\b|\bclinicians?\b|\bnurs(es?|ing)\b|\bproviders? (network|organization|group)s?\b|healthcare providers?|care providers?", re.I), "clinicians"),
    (re.compile(r"\bmedicare\b|\bmedicaid\b", re.I), "Medicare/Medicaid"),
    (re.compile(r"medical (device|record|claim|imaging|billing|coding)s?|\bmedtech\b", re.I), "medical"),
    (re.compile(r"revenue cycle|prior auth|claims (processing|adjudication)|\brcm\b", re.I), "RCM/claims"),
    (re.compile(r"\bpharma(ceutical)?s?\b|life sciences?|\bbiotech\b|clinical trials?|\bfda\b", re.I), "pharma/life sci"),
    (re.compile(r"telehealth|telemedicine|digital health|virtual care|care (delivery|management|coordination|team)s?", re.I), "digital health/care"),
    (re.compile(r"physical therap|rehabilitation|\bpt/ot\b|occupational therap", re.I), "therapy/rehab"),
    (re.compile(r"\bdiagnos(tic|is|es)\b|\bpathology\b|\bradiology\b|\bgenomics?\b", re.I), "diagnostics"),
]
HEALTH_ADJ_MIN = 2          # distinct terms needed for "adj"
HEALTH_CORE_SECTORS = {"healthtech", "medtech"}

def find_health_hits(text):
    """Distinct healthcare terms in a posting, in HEALTH_TERMS order."""
    if not text:
        return []
    return [label for rx, label in HEALTH_TERMS if rx.search(text)]

def compute_health(job):
    """Fill job.health / job.health_hits. Idempotent."""
    job.health_hits = find_health_hits(f"{job.title} {job.description or job.snippet}")
    if job.sector in HEALTH_CORE_SECTORS:
        job.health = "core"
    elif len(job.health_hits) >= HEALTH_ADJ_MIN:
        job.health = "adj"
    else:
        job.health = ""
    return job.health

def score_job(job, fp=None):
    """Everything derived from a Job's text: fit score + healthcare signal."""
    job.fit = compute_fit(job, fp)
    compute_health(job)

def set_fit_profile(name):
    global ACTIVE_FIT
    ACTIVE_FIT = FIT_PROFILES[name]

def compute_fit(job, fp=None):
    """0-100 resume-fit score for a Job under a fit profile."""
    fp = fp or ACTIVE_FIT
    score = 0
    for rx, pts in fp["level"]:
        if rx.search(job.title):
            score += pts
            break
    blob = f"{job.title} {job.description or job.snippet}"
    for rx, per, cap in fp["domains"]:
        score += min(len(rx.findall(blob)) * per, cap)
    score += fp["sector"].get(job.sector, 0)
    if job.remote == "remote":
        score += fp["remote_bonus"]
    if fp["location_re"].search(job.location or ""):
        score += fp["location_bonus"]
    return min(score, 100)

def fetch_json(url, timeout=15):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()

_BLOCK_END_RE = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|section|article|blockquote)\s*>|<br\s*/?>", re.I)
_LI_RE = re.compile(r"<li\b[^>]*>", re.I)
_HEAD_RE = re.compile(r"<(h[1-6]|strong|b)\b[^>]*>", re.I)

def strip_html(s):
    """HTML -> plain text, keeping the block structure as newlines.

    Paragraph/list/heading boundaries become line breaks and <li> gets a
    bullet, so the dashboard's expanded description reads like the original
    posting instead of one wall of text. Whitespace inside a line is still
    collapsed. Text that starts mid-tag (LinkedIn hands back a fragment that
    opens with an attribute tail like `...clamp-after-5">`) has that junk
    trimmed.
    """
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r'^[^<>]{0,200}?">', "", s)          # leading attribute tail
    s = _LI_RE.sub("\n• ", s)
    s = _BLOCK_END_RE.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\xa0", " ").replace("\u200d", "")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"\n\n(?=• )", "\n", s)             # keep list items contiguous
    return s.strip()

def title_matches_for(title, level, domain, exclude):
    t = title.lower()
    if any(re.search(p, t) for p in exclude):
        return False
    if not any(re.search(p, t) for p in level):
        return False
    return any(re.search(p, t) for p in domain)

def title_matches(title):
    return title_matches_for(
        title, LEVEL_PATTERNS, DOMAIN_PATTERNS, EXCLUDE_TITLE_PATTERNS)

# ETL data-infra company whitelist — these always pass the signal check
ETL_COMPANIES = {
    "fivetran", "hightouch", "airbyte", "materialize", "starburst",
    "sigmacomputing", "motherduck", "omni", "cribl", "stitch", "ascend",
    "dremio", "clickhouse", "prismatic", "workato", "merge",
    "confluent", "ascend-io", "astronomer", "prefect", "anomalo",
    "redpanda-data", "coalesce", "polytomic", "paragon", "nango",
    "datafold", "collibra", "atlan", "bigid", "elastic", "mongodb",
    "honeycomb", "unstructured", "hevodata", "tinybird", "matillion", "acceldata",
}

def has_etl_signal(company, title, content):
    """For the etl profile: keep only roles in data-infra companies OR with ETL keywords in title/content."""
    if company in ETL_COMPANIES:
        return True
    blob = (title + " " + content).lower()
    return any(kw in blob for kw in ETL_KEYWORDS)

PAY_RANGE_RE = re.compile(
    r"""
    \$\s*(\d{2,3}(?:,\d{3})*|\d{2,3}(?:\.\d+)?[kK])    # min e.g. $158,000 or $80K
    \s*(?:-|–|—|to|and)\s*
    \$?\s*(\d{2,3}(?:,\d{3})*|\d{2,3}(?:\.\d+)?[kK])   # max
    """, re.VERBOSE,
)

def parse_pay_amt(s):
    s = s.replace(",", "").lower()
    if s.endswith("k"):
        return int(float(s[:-1]) * 1000)
    return int(float(s))

def extract_pay(text):
    """Find the highest plausible salary range in description text. Returns (min, max, raw)."""
    if not text:
        return 0, 0, ""
    candidates = []
    for m in PAY_RANGE_RE.finditer(text):
        try:
            lo = parse_pay_amt(m.group(1))
            hi = parse_pay_amt(m.group(2))
        except ValueError:
            continue
        # filter out hourly rates, equity %, irrelevant ranges
        if lo < 30000 or hi < 30000 or hi > 1_000_000 or hi < lo:
            continue
        candidates.append((lo, hi, m.group(0)))
    if not candidates:
        return 0, 0, ""
    # Pick the range with the highest max (usually the base salary band, not perks)
    best = max(candidates, key=lambda c: c[1])
    return best[0], best[1], best[2]

def classify_remote(location, content="", workplace_type=""):
    """Return 'remote', 'hybrid', 'onsite', or '' from text signals."""
    blob = f"{location} {workplace_type}".lower()
    if "remote" in blob and "hybrid" not in blob:
        return "remote"
    if "hybrid" in blob:
        return "hybrid"
    if workplace_type:
        wt = workplace_type.lower()
        if wt in ("onsite", "on-site", "in-office", "office"):
            return "onsite"
    # fall back to scanning description
    low = content.lower()
    if re.search(r"\bfully\s+remote\b|\b100%\s+remote\b|\bremote[-\s]first\b|\bremote,?\s+(usa|united states)", low):
        return "remote"
    if re.search(r"\bhybrid\b", low):
        return "hybrid"
    return ""

def find_ai_hits(text):
    if not text:
        return [], ""
    low = text.lower()
    hits = sorted({kw for kw in AI_KEYWORDS if kw in low})
    snippet = ""
    if hits:
        first = hits[0]
        idx = low.find(first)
        start = max(0, idx - 80)
        end = min(len(text), idx + len(first) + 160)
        snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
    return hits, snippet

def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        data = fetch_json(url)
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not title_matches(title):
            continue
        content = strip_html(j.get("content", ""))
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = (j.get("location") or {}).get("name", "")
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="greenhouse", title=title, location=loc,
            url=j.get("absolute_url", ""), ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=j.get("first_published", "") or "",
            updated_at=j.get("updated_at", "") or "",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        data = fetch_json(url)
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in data:
        title = j.get("text", "")
        if not title_matches(title):
            continue
        desc_parts = [j.get("descriptionPlain", "")]
        for ll in j.get("lists", []):
            desc_parts.append(strip_html(ll.get("content", "")))
        desc_parts.append(j.get("additionalPlain", ""))
        content = " ".join(p for p in desc_parts if p)
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = (j.get("categories") or {}).get("location", "")
        ts = j.get("createdAt") or 0
        posted_iso = ""
        if ts:
            from datetime import datetime, timezone
            posted_iso = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="lever", title=title, location=loc,
            url=j.get("hostedUrl", ""), ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=posted_iso,
            updated_at=posted_iso,
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, j.get("workplaceType", "")),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    try:
        data = fetch_json(url)
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not title_matches(title):
            continue
        content = strip_html(j.get("descriptionHtml") or j.get("descriptionPlain", ""))
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = j.get("locationName", "") or j.get("location", "")
        pay_min = pay_max = 0
        pay_text = ""
        comp = j.get("compensation") or {}
        for tier in comp.get("summaryComponents") or []:
            if tier.get("compensationType") == "Salary" and tier.get("interval") == "1 YEAR":
                lo = tier.get("minValue") or 0
                hi = tier.get("maxValue") or 0
                if hi > pay_max:
                    pay_min, pay_max = lo, hi
        if not pay_max:
            pay_text = comp.get("scrapeableCompensationSalarySummary", "") or comp.get("compensationTierSummary", "") or ""
            pmin, pmax, _ = extract_pay(pay_text or content)
            if pmax:
                pay_min, pay_max = pmin, pmax
        else:
            pay_text = comp.get("scrapeableCompensationSalarySummary", "") or f"${pay_min:,} - ${pay_max:,}"
        wt = j.get("workplaceType", "") or ("Remote" if j.get("isRemote") else "")
        out.append(Job(
            company=slug, ats="ashby", title=title, location=loc,
            url=j.get("jobUrl", ""), ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=j.get("publishedAt", "") or "",
            updated_at=j.get("publishedAt", "") or "",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, wt),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

# Workday list pages are cheap (20 postings/request, title + path only), so walk
# the whole board rather than sampling the first 200 — big tenants (Northrop 3.4k,
# Thermo Fisher 3.2k) sort retail/field roles first and eng roles would never
# surface. Detail fetches still happen only for title matches. searchText was
# tried and rejected: Workday search is fuzzy OR-matching, so "director
# engineering" buries actual Director titles under thousands of loose hits.
WORKDAY_MAX_POSTINGS = 4000

def fetch_workday(config):
    """
    Fetch from a Workday tenant. config is a dict:
      {"slug": "nvidia", "tenant": "nvidia", "wd": "wd5", "site": "NVIDIAExternalCareerSite"}
    Walks the full board (capped at WORKDAY_MAX_POSTINGS), filters by title,
    then fetches detail for matches. Retries once on 429.
    """
    slug = config["slug"]
    tenant, wd, site = config["tenant"], config["wd"], config["site"]
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    list_url = f"{base}/jobs"
    out = []
    seen = set()
    try:
        for offset in range(0, WORKDAY_MAX_POSTINGS, 20):
            r = requests.post(list_url,
                              json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                              headers={**UA, "Accept": "application/json", "Content-Type": "application/json"},
                              timeout=15)
            if r.status_code == 429:
                time.sleep(2)
                r = requests.post(list_url,
                                  json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                                  headers={**UA, "Accept": "application/json", "Content-Type": "application/json"},
                                  timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            postings = data.get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                ext = p.get("externalPath", "")
                if not ext or ext in seen:
                    continue
                title = p.get("title", "")
                if not title_matches(title):
                    continue
                seen.add(ext)
                # Fetch detail for description
                try:
                    dr = requests.get(base + ext, headers={**UA, "Accept": "application/json"}, timeout=12)
                    if dr.status_code != 200:
                        continue
                    detail = dr.json().get("jobPostingInfo", {})
                except Exception:
                    continue
                content = strip_html(detail.get("jobDescription", ""))
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                loc = p.get("locationsText", "") or detail.get("location", "")
                pay_min, pay_max, pay_text = extract_pay(content)
                # Workday gives ISO startDate
                posted_iso = detail.get("startDate", "") or ""
                if posted_iso and len(posted_iso) == 10:
                    posted_iso += "T00:00:00Z"
                ext_url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{ext}"
                out.append(Job(
                    company=slug, ats="workday", title=title, location=loc,
                    url=ext_url, ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet,
                    description=content,
                    posted_at=posted_iso, updated_at=posted_iso,
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            if len(postings) < 20:
                break
    except Exception:
        pass
    return out

def fetch_workday_wrapper(slug_or_config):
    """Lookup config from companies.json structure: workday list contains config dicts."""
    if isinstance(slug_or_config, dict):
        return fetch_workday(slug_or_config)
    return []

# Accenture's Elastic search needs a plain keyword to narrow server-side. A tab
# can set "accenture_keyword" explicitly; otherwise its label is used.
def accenture_keyword(key):
    p = PROFILES.get(key) or {}
    kw = p.get("accenture_keyword") or p.get("label") or key
    # "Senior SWE / PM — Healthtech (Remote)" -> "senior swe": the label's
    # first clause is the role; the rest is qualifiers Accenture can't use.
    kw = re.split(r"\s+[—–-]\s+|/|\(", kw, 1)[0]
    kw = re.sub(r"[^a-z0-9 ]+", " ", kw.lower())
    return re.sub(r"\s+", " ", kw).strip() or "engineer"


def accenture_keywords():
    keys = [k for k, v in PROFILES.items() if profile_source(v) == "ats"]
    return sorted({accenture_keyword(k) for k in keys} or {"engineer"})


ACCENTURE_KEYWORD = "engineer"  # mutated in main()

def fetch_accenture(slug, keyword=None):
    """
    Accenture exposes a custom Elastic-backed search at
    /api/accenture/elastic/findjobs (multipart/form-data POST). Pulls newest
    matching postings, filters by title, then keeps roles with AI signal.
    """
    keyword = keyword or ACCENTURE_KEYWORD
    api = "https://www.accenture.com/api/accenture/elastic/findjobs"
    page_size = 50
    max_pages = 10
    out = []
    seen_ids = set()
    try:
        for page in range(max_pages):
            fields = {
                "startIndex":               str(page * page_size),
                "maxResultSize":            str(page_size),
                "jobKeyword":               keyword,
                "jobCountry":               "",
                "jobLanguage":              "en",
                "countrySite":              "us-en",
                "sortBy":                   "1",          # 1 = newest
                "searchType":               "vectorSearch",
                "enableQueryBoost":         "true",
                "minScore":                 "0",
                "getFeedbackJudgmentEnabled": "true",
                "useCleanEmbedding":        "true",
                "score":                    "true",
                "totalHits":                "true",
                "debugQuery":               "false",
                "jobFilters":               "[]",
            }
            r = requests.post(
                api,
                files={k: (None, v) for k, v in fields.items()},
                headers={**UA, "Accept": "application/json",
                         "Referer": "https://www.accenture.com/us-en/careers/jobsearch"},
                timeout=20,
            )
            if r.status_code != 200:
                break
            data = r.json().get("data") or []
            if not data:
                break
            for j in data:
                title = j.get("title", "")
                if not title_matches(title):
                    continue
                # Description text lives across a few fields; concat them.
                content = " ".join(strip_html(j.get(f, "")) for f in (
                    "descriptionClean", "description", "qualificationClean",
                    "qualification", "responsibilitiesClean", "responsibilities",
                ) if j.get(f))
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                detail_url = (j.get("jobDetailUrl") or "").replace("{0}", "us-en")
                job_id = j.get("jobId") or j.get("requisitionId") or detail_url
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                country = j.get("country", "")
                city = j.get("city") or ""
                loc = ", ".join(x for x in (city, country) if x)
                pay_min, pay_max, pay_text = extract_pay(content)
                posted_iso = j.get("postedDate", "") or j.get("updateDate", "") or ""
                out.append(Job(
                    company=slug, ats="accenture", title=title, location=loc,
                    url=detail_url, ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet,
                    description=content,
                    posted_at=posted_iso, updated_at=j.get("updateDate", "") or posted_iso,
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            if len(data) < page_size:
                break
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        pass
    return out

# apply.workable.com rate-limits hard per IP (429 wall within minutes at 10
# concurrent). Throttle all workable calls through one gate with pacing.
_WORKABLE_SEM = threading.Semaphore(2)
_WORKABLE_PACE = 0.6


def _workable_req(method, url, json_body=None, tries=4):
    with _WORKABLE_SEM:
        for i in range(tries):
            r = requests.request(method, url, json=json_body,
                                 headers={**UA, "Accept": "application/json"},
                                 timeout=20)
            if r.status_code == 429:
                time.sleep(30 * (i + 1))
                continue
            time.sleep(_WORKABLE_PACE)
            return r
    return None


def fetch_workable(config):
    """
    Workable per-account API. List: POST apply.workable.com/api/v3/accounts/
    <slug>/jobs (10/page, {"token": <nextPage>} to paginate; location.
    countryCode is exact). Detail: GET .../api/v2/accounts/<slug>/jobs/
    <shortcode> for description/requirements/benefits, fetched only for
    title-matched jobs. The jobs.workable.com aggregator this used to scrape
    now 429s anonymous clients, while v3 works again (re-checked 2026-07-18).

    companies.json entries are plain slugs or legacy {"slug", "name"} dicts.
    """
    slug = config["slug"] if isinstance(config, dict) else config
    base = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    out = []
    try:
        jobs_list = []
        token = None
        for _ in range(30):
            r = _workable_req("POST", base, {**body, "token": token} if token else body)
            if r is None or r.status_code != 200:
                break
            data = r.json()
            page = data.get("results") or []
            jobs_list += page
            token = data.get("nextPage")
            if not token or not page:
                break
        for j in jobs_list:
            title = j.get("title", "")
            if not title_matches(title):
                continue
            shortcode = j.get("shortcode", "")
            detail = {}
            if shortcode:
                rd = _workable_req(
                    "GET", f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}")
                if rd is not None and rd.status_code == 200:
                    detail = rd.json()
            content = strip_html(
                (detail.get("description") or "") + " " +
                (detail.get("requirements") or "") + " " +
                (detail.get("benefits") or "")
            )
            hits, snippet = find_ai_hits(content)
            if not hits and REQUIRE_AI_HITS:
                continue
            if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                continue
            locs = j.get("locations") or []
            loc = "; ".join(", ".join(b for b in (L.get("city"), L.get("region"), L.get("country")) if b)
                            for L in locs if isinstance(L, dict)) or \
                  ", ".join(b for b in ((j.get("location") or {}).get("city"),
                                        (j.get("location") or {}).get("country")) if b)
            pay_min, pay_max, pay_text = extract_pay(content)
            wt = "Remote" if j.get("remote") else ""
            out.append(Job(
                company=slug, ats="workable", title=title, location=loc,
                url=f"https://apply.workable.com/{slug}/j/{shortcode}/",
                ai_hits=hits,
                ai_hit_count=sum(content.lower().count(h) for h in hits),
                snippet=snippet,
                description=content,
                posted_at=(j.get("published") or "")[:10],
                updated_at="",
                pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                remote=classify_remote(loc, content, wt),
                stage=get_meta(slug)[0], sector=get_meta(slug)[1],
            ))
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        pass
    return out

def fetch_teamtailor(config):
    """
    Teamtailor exposes a JSON Feed at <career_url>/jobs.json. Career URL is
    usually a custom domain (e.g. careers.oda.com), so entries in companies.json
    look like {"slug": "oda", "url": "https://careers.oda.com"}. Plain strings
    are also accepted and assumed to be at https://<slug>.teamtailor.com.
    """
    if isinstance(config, dict):
        slug = config["slug"]
        base = config.get("url") or f"https://{slug}.teamtailor.com"
    else:
        slug = config
        base = f"https://{slug}.teamtailor.com"
    base = base.rstrip("/")
    try:
        data = fetch_json(f"{base}/jobs.json")
    except (requests.RequestException, json.JSONDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        title = item.get("title", "")
        if not title_matches(title):
            continue
        posting = item.get("_jobposting") or {}
        content = strip_html(posting.get("description") or item.get("content_html", ""))
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = ""
        jl = posting.get("jobLocation")
        if isinstance(jl, dict):
            addr = jl.get("address") or {}
            loc = ", ".join(b for b in (addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")) if b)
        elif isinstance(jl, list) and jl:
            parts = []
            for L in jl:
                addr = (L or {}).get("address") or {}
                parts.append(", ".join(b for b in (addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")) if b))
            loc = "; ".join(p for p in parts if p)
        pay_min = pay_max = 0
        pay_text = ""
        bs = posting.get("baseSalary") or {}
        v = bs.get("value") or {}
        try:
            mn = int(v.get("minValue") or 0)
            mx = int(v.get("maxValue") or 0)
            if mx and v.get("unitText") in (None, "YEAR", "Year"):
                pay_min, pay_max = mn, mx
                pay_text = f"${mn:,} - ${mx:,}"
        except (TypeError, ValueError):
            pass
        if not pay_max:
            pay_min, pay_max, pay_text = extract_pay(content)
        wt = "Remote" if (posting.get("jobLocationType") == "TELECOMMUTE") else ""
        out.append(Job(
            company=slug, ats="teamtailor", title=title, location=loc,
            url=item.get("url", ""), ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=item.get("date_published", "") or posting.get("datePosted", "") or "",
            updated_at=item.get("date_modified", "") or "",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, wt),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

def fetch_bamboohr(slug):
    """
    BambooHR careers API: GET <slug>.bamboohr.com/careers/list (needs
    Accept: application/json) lists openings; /careers/<id>/detail has the
    description + compensation. Detail is fetched only for title-matched jobs.
    atsLocation carries full country/state names (no ISO-code trap).
    """
    base = f"https://{slug}.bamboohr.com/careers"
    headers = {**UA, "Accept": "application/json"}
    try:
        r = requests.get(f"{base}/list", headers=headers, timeout=15)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return []
        jobs = r.json().get("result") or []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in jobs:
        title = j.get("jobOpeningName", "")
        if not title_matches(title):
            continue
        jid = j.get("id")
        detail = {}
        try:
            rd = requests.get(f"{base}/{jid}/detail", headers=headers, timeout=15)
            if rd.status_code == 200 and rd.text.strip().startswith("{"):
                detail = (rd.json().get("result") or {}).get("jobOpening") or {}
        except (requests.RequestException, json.JSONDecodeError):
            pass
        content = strip_html(detail.get("description") or "")
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        ats = detail.get("atsLocation") or j.get("atsLocation") or {}
        loc = ", ".join(x for x in (ats.get("city"), ats.get("state") or ats.get("province"),
                                    ats.get("country")) if x)
        comp = detail.get("compensation") or ""
        pay_min, pay_max, pay_text = extract_pay(content + " " + str(comp))
        wt = "Remote" if j.get("isRemote") else ""
        out.append(Job(
            company=slug, ats="bamboohr", title=title, location=loc,
            url=detail.get("jobOpeningShareUrl") or f"{base}/{jid}",
            ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=(detail.get("datePosted") or "")[:10],
            updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, wt),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

def fetch_breezy(slug):
    """
    Breezy HR: GET <slug>.breezy.hr/json lists positions (location.country.id
    is an exact ISO code). No JSON detail endpoint — the position page is
    server-rendered, description sits between class="description" and
    class="apply. Detail fetched only for title-matched jobs.
    """
    try:
        r = requests.get(f"https://{slug}.breezy.hr/json", headers=UA, timeout=15)
        if r.status_code != 200 or not r.text.strip().startswith("["):
            return []
        jobs = r.json()
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in jobs:
        title = j.get("name", "")
        if not title_matches(title):
            continue
        url = j.get("url", "")
        content = ""
        if url:
            try:
                rd = requests.get(url, headers=UA, timeout=15)
                if rd.status_code == 200:
                    i = rd.text.find('class="description"')
                    if i >= 0:
                        seg = rd.text[i:]
                        end = seg.find('class="apply')
                        if end < 0:
                            end = seg.find("<script")
                        content = strip_html(seg[:end if end > 0 else 20000])
            except requests.RequestException:
                pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc_obj = j.get("location") or {}
        loc = loc_obj.get("name", "")
        pay_min, pay_max, pay_text = extract_pay(content)
        sal = j.get("salary")
        if not pay_max and isinstance(sal, dict):
            try:
                mn, mx = int(sal.get("min") or 0), int(sal.get("max") or 0)
                if mx > 10000:
                    pay_min, pay_max = mn, mx
                    pay_text = f"${mn:,} - ${mx:,}"
            except (TypeError, ValueError):
                pass
        wt = "Remote" if loc_obj.get("is_remote") else ""
        out.append(Job(
            company=slug, ats="breezy", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=(j.get("published_date") or "")[:10],
            updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, wt),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

JAZZHR_ITEM_RE = re.compile(
    r"<h3 class='list-group-item-heading'>\s*<a href=\"([^\"]*/apply/[A-Za-z0-9]+/[^\"]*)\">\s*"
    r"([^<]+?)\s*</a>.*?(?:fa-map-marker'></i>([^<]*)<|</ul>)", re.S)

def fetch_jazzhr(slug):
    """
    JazzHR: <slug>.applytojob.com/apply/ is HTML-only; job detail pages embed
    a JSON-LD JobPosting with the full description. Detail fetched only for
    title-matched jobs. Locations are human-readable "City, ST".
    """
    try:
        r = requests.get(f"https://{slug}.applytojob.com/apply/", headers=UA,
                         timeout=15, allow_redirects=True)
        if r.status_code != 200 or "applytojob.com" not in r.url:
            return []
    except requests.RequestException:
        return []
    out = []
    for url, title, loc in JAZZHR_ITEM_RE.findall(r.text):
        title = title.strip()
        if not title_matches(title):
            continue
        content, posted = "", ""
        try:
            rd = requests.get(url, headers=UA, timeout=15)
            for blob in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                                   rd.text, re.S):
                try:
                    ld = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
                    content = strip_html(ld.get("description") or "")
                    posted = (ld.get("datePosted") or "")[:10]
                    break
        except requests.RequestException:
            pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = (loc or "").strip()
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="jazzhr", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=posted, updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

JOBVITE_JOB_RE = re.compile(
    r'class="jv-job-list-name"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', re.S)
JOBVITE_LOC_RE = re.compile(r'class="jv-job-list-location"[^>]*>([^<]+)<', re.S)

def fetch_jobvite(slug):
    """
    Jobvite: jobs.jobvite.com/<slug>/jobs/all is HTML-only; detail pages embed
    a JSON-LD JobPosting with the description. Detail fetched only for
    title-matched jobs.
    """
    try:
        r = requests.get(f"https://jobs.jobvite.com/{slug}/jobs/all", headers=UA,
                         timeout=20, allow_redirects=True)
        if r.status_code != 200 or "jobs.jobvite.com" not in r.url:
            return []
    except requests.RequestException:
        return []
    names = JOBVITE_JOB_RE.findall(r.text)
    locs = [re.sub(r"\s+", " ", L).strip() for L in JOBVITE_LOC_RE.findall(r.text)]
    out = []
    for i, (href, title) in enumerate(names):
        title = title.strip()
        if not title_matches(title):
            continue
        url = href if href.startswith("http") else f"https://jobs.jobvite.com{href}"
        content, posted = "", ""
        try:
            rd = requests.get(url, headers=UA, timeout=20)
            m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                          rd.text, re.S)
            if m:
                try:
                    ld = json.loads(m.group(1))
                    if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
                        content = strip_html(ld.get("description") or "")
                        posted = (ld.get("datePosted") or "")[:10]
                except json.JSONDecodeError:
                    pass
        except requests.RequestException:
            pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = locs[i] if i < len(locs) else ""
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="jobvite", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=posted, updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

def fetch_rippling(slug):
    """
    Rippling ATS public board API. List endpoint returns title/url/location only;
    descriptions require one detail call per job, so title-filter first to keep
    the request count down.
    """
    base = f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
    try:
        data = fetch_json(base)
    except (requests.RequestException, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        title = j.get("name", "")
        if not title_matches(title):
            continue
        uuid = j.get("uuid", "")
        detail = {}
        if uuid:
            try:
                detail = fetch_json(f"{base}/{uuid}")
            except (requests.RequestException, json.JSONDecodeError):
                detail = {}
        desc = detail.get("description") or {}
        if isinstance(desc, dict):
            content = strip_html(" ".join(v for v in desc.values() if v))
        else:
            content = strip_html(str(desc))
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        locs = detail.get("workLocations") or []
        loc = ", ".join(locs) if locs else (j.get("workLocation") or {}).get("label", "")
        pay_min, pay_max, pay_text = extract_pay(content)
        posted = detail.get("createdOn", "") or ""
        out.append(Job(
            company=slug, ats="rippling", title=title, location=loc,
            url=j.get("url", ""), ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet,
            description=content,
            posted_at=posted,
            updated_at=posted,
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out

def fetch_smartrecruiters(slug):
    """
    SmartRecruiters public postings API. Slugs are CamelCase company identifiers
    (case-sensitive in the API path), e.g. "AbbVie", "NBCUniversal3".
    Lists all postings (100/page), filters by title, then fetches detail for
    matches (description lives behind a per-posting endpoint).
    """
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    out = []
    offset = 0
    try:
        while True:
            r = requests.get(base, params={"limit": 100, "offset": offset},
                             headers=UA, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            content_list = data.get("content", []) or []
            if not content_list:
                break
            for p in content_list:
                title = p.get("name", "")
                if not title_matches(title):
                    continue
                pid = p.get("id", "")
                if not pid:
                    continue
                try:
                    dr = requests.get(f"{base}/{pid}", headers=UA, timeout=12)
                    if dr.status_code != 200:
                        continue
                    detail = dr.json()
                except Exception:
                    continue
                sections = (detail.get("jobAd") or {}).get("sections", {})
                content = strip_html(" ".join(
                    (sections.get(k) or {}).get("text", "")
                    for k in ("jobDescription", "qualifications", "additionalInformation")))
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                locd = p.get("location") or {}
                loc = locd.get("fullLocation", "") or ", ".join(
                    x for x in (locd.get("city", ""), (locd.get("country") or "").upper()) if x)
                posted = p.get("releasedDate", "") or ""
                pay_min, pay_max, pay_text = extract_pay(content)
                remote = "remote" if locd.get("remote") else classify_remote(loc, content)
                out.append(Job(
                    company=slug, ats="smartrecruiters", title=title, location=loc,
                    url=detail.get("postingUrl", "") or f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                    ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet,
                    description=content,
                    posted_at=posted, updated_at=posted,
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=remote,
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            offset += 100
            if offset >= data.get("totalFound", 0):
                break
    except Exception:
        pass
    return out

def fetch_jibe(config):
    """
    Jibe (iCIMS Careers Cloud) JSON API. config is a dict:
      {"slug": "amd", "base": "https://careers.amd.com"}
    GET <base>/api/jobs?page=N returns jobs with the description inline,
    so no per-posting detail fetch is needed.
    """
    slug, base = config["slug"], config["base"].rstrip("/")
    out = []
    page = 1
    fetched = 0
    try:
        while True:
            r = requests.get(f"{base}/api/jobs", params={"page": page, "limit": 100},
                             headers={**UA, "Accept": "application/json"}, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            jobs = data.get("jobs", [])
            if not jobs:
                break
            for wrap in jobs:
                j = wrap.get("data", wrap)
                title = j.get("title", "")
                if not title_matches(title):
                    continue
                content = strip_html(j.get("description", ""))
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                loc = j.get("full_location", "") or ", ".join(
                    x for x in (j.get("city", ""), j.get("country", "")) if x)
                posted = j.get("create_date", "") or ""
                pay_min, pay_max, pay_text = extract_pay(content)
                out.append(Job(
                    company=slug, ats="jibe", title=title, location=loc,
                    url=j.get("apply_url", "") or base,
                    ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet,
                    description=content,
                    posted_at=posted, updated_at=posted,
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            fetched += len(jobs)
            if fetched >= data.get("totalCount", 0):
                break
            page += 1
    except Exception:
        pass
    return out

# --------------------------------------------------------------------------
# Round-16 ATS fetchers (iCIMS / UKG UltiPro / Paylocity / Dayforce /
# Recruitee / Gem / Comeet / Jobscore / Trakstar / ApplicantPro / Pinpoint).
# Endpoint notes live in _new_companies_deep_search_round16.md.

JSON_STR_RE = re.compile(r'"[Dd]escription":\s*"((?:[^"\\]|\\.)*)"')


def _json_str(m):
    """Decode a JSON string literal captured without its quotes."""
    try:
        return json.loads('"' + m.group(1) + '"')
    except Exception:
        return ""


LD_JSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _ld_jobposting(txt):
    for ld in LD_JSON_RE.findall(txt):
        try:
            d = json.loads(ld)
        except Exception:
            continue
        for obj in (d if isinstance(d, list) else [d]):
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                return obj
    return {}


ICIMS_CARD_RE = re.compile(r'class="iCIMS_JobCardItem"(.*?)(?=class="iCIMS_JobCardItem"|</ul>)', re.S)
ICIMS_JOB_RE = re.compile(
    r'href="([^"]*?/jobs/(\d+)/[^/"]+/job)[^"]*"[^>]*title="\d+\s*-\s*([^"]+)"', re.I)
ICIMS_H3_RE = re.compile(r'<h[23][^>]*>\s*(.*?)\s*</h[23]>', re.S)
ICIMS_LOC_RE = re.compile(r'Job Locations?</span>\s*<span[^>]*>\s*([^<]+)', re.S)
ICIMS_PAGES_RE = re.compile(r'Page\s+\d+\s+of\s+(\d+)')


def fetch_icims(slug):
    """
    Classic iCIMS portal. The outer /jobs/search page is a shell; the job
    table lives in the in_iframe=1 iframe (plain HTML). Job detail pages
    carry a JSON-LD JobPosting with full description + address. Subdomains
    that are login portals / talentbrew / jibe sites parse out to 0 links.
    """
    base = (f"https://{slug}.icims.com/jobs/search?ss=1&in_iframe=1&mobile=false"
            f"&width=1200&height=500&bga=true&needsRedirect=false"
            f"&jan1offset=-300&jun1offset=-240")
    out = []
    seen = set()
    try:
        r = requests.get(base, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        npages_m = ICIMS_PAGES_RE.search(r.text)
        npages = min(int(npages_m.group(1)) if npages_m else 1, 10)
        pages = [r.text]
        for p in range(1, npages):
            rp = requests.get(base + f"&pr={p}", headers=UA, timeout=20)
            if rp.status_code != 200:
                break
            pages.append(rp.text)
    except requests.RequestException:
        return []
    for ptxt in pages:
        for card in ICIMS_CARD_RE.findall(ptxt):
            m = ICIMS_JOB_RE.search(card)
            if m:
                url, jid, title = m.group(1), m.group(2), m.group(3)
            else:
                m2 = re.search(r'href="([^"]*?/jobs/(\d+)/[^/"]+/job)[^"]*"', card)
                h3 = ICIMS_H3_RE.search(card)
                if not m2 or not h3:
                    continue
                url, jid = m2.group(1), m2.group(2)
                title = re.sub(r"<[^>]+>", "", h3.group(1))
            title = html.unescape(title).strip()
            if jid in seen or not title:
                continue
            seen.add(jid)
            if not title_matches(title):
                continue
            lm = ICIMS_LOC_RE.search(card)
            loc = html.unescape(lm.group(1)).strip() if lm else ""
            content, posted = "", ""
            try:
                rd = requests.get(url + "?in_iframe=1", headers=UA, timeout=20)
                if rd.status_code == 200:
                    ld = _ld_jobposting(rd.text)
                    content = strip_html(ld.get("description", ""))
                    posted = (ld.get("datePosted") or "")[:10]
            except requests.RequestException:
                pass
            hits, snippet = find_ai_hits(content)
            if not hits and REQUIRE_AI_HITS:
                continue
            if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                continue
            pay_min, pay_max, pay_text = extract_pay(content)
            out.append(Job(
                company=slug, ats="icims", title=title, location=loc,
                url=url, ai_hits=hits,
                ai_hit_count=sum(content.lower().count(h) for h in hits),
                snippet=snippet, description=content,
                posted_at=posted, updated_at="",
                pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                remote=classify_remote(loc, content),
                stage=get_meta(slug)[0], sector=get_meta(slug)[1],
            ))
    return out


ULTIPRO_SEARCH_BODY = {
    "opportunitySearch": {"Top": 100, "Skip": 0, "QueryString": "",
                          "OrderBy": [{"Value": "postedDateDesc",
                                       "PropertyName": "PostedDate",
                                       "Ascending": False, "Sequence": 1}],
                          "Filters": []},
    "matchCriteria": {"PreferredJobs": [], "Educations": [],
                      "LicenseAndCertifications": [], "Skills": [],
                      "hasNoLicenses": False, "SkippedSkills": []}}


def fetch_ultipro(config):
    """
    UKG Pro Recruiting (recruiting.ultipro.com). config:
      {"slug": "usaa-ins", "tenant": "AAA1002AWCN", "board": "<guid>"}
    POST JobBoardView/LoadSearchResults pages the board (Top=100); the
    OpportunityDetail HTML embeds the full description as a JSON string.
    Locations carry exact Country.Code ("USA").
    """
    slug, tenant, board = config["slug"], config["tenant"], config["board"]
    base = f"https://recruiting.ultipro.com/{tenant}/JobBoard/{board}"
    out = []
    skip = 0
    try:
        while skip < 500:
            body = json.loads(json.dumps(ULTIPRO_SEARCH_BODY))
            body["opportunitySearch"]["Skip"] = skip
            r = requests.post(f"{base}/JobBoardView/LoadSearchResults",
                              json=body, headers=UA, timeout=20)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                break
            d = r.json()
            opps = d.get("opportunities") or []
            if not opps:
                break
            for o in opps:
                title = o.get("Title", "")
                if not title_matches(title):
                    continue
                oid = o.get("Id", "")
                url = f"{base}/OpportunityDetail?opportunityId={oid}"
                content = strip_html(o.get("BriefDescription") or "")
                try:
                    rd = requests.get(url, headers=UA, timeout=20)
                    if rd.status_code == 200:
                        m = JSON_STR_RE.search(rd.text)
                        if m:
                            content = strip_html(_json_str(m))
                except requests.RequestException:
                    pass
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                parts = []
                for L in (o.get("Locations") or []):
                    ad = (L.get("Address") or {})
                    parts.append(", ".join(str(x) for x in
                                           (ad.get("City"),
                                            (ad.get("State") or {}).get("Code"),
                                            (ad.get("Country") or {}).get("Code")) if x))
                loc = "; ".join(p for p in parts if p)
                pay_min, pay_max, pay_text = extract_pay(content)
                out.append(Job(
                    company=slug, ats="ultipro", title=title, location=loc,
                    url=url, ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet, description=content,
                    posted_at=(o.get("PostedDate") or "")[:10], updated_at="",
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content,
                                           str(o.get("JobLocationType") or "")),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            skip += len(opps)
            if skip >= (d.get("totalCount") or 0):
                break
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return out


PAYLOCITY_DATA_RE = re.compile(r"window\.pageData\s*=\s*(\{.*?\});", re.S)


def fetch_paylocity(config):
    """
    Paylocity Recruiting. config: {"slug": "acme", "guid": "<board guid>"}
    The /Recruiting/Jobs/All/<guid> page embeds window.pageData with the job
    list (JobLocation.Country is exact "USA"). Detail pages server-render the
    description after a 'Description</div>' marker.
    """
    slug, guid = config["slug"], config["guid"]
    out = []
    try:
        r = requests.get(f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{guid}",
                         headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        m = PAYLOCITY_DATA_RE.search(r.text)
        if not m:
            return []
        jobs = (json.loads(m.group(1)).get("Jobs")) or []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    for j in jobs:
        title = j.get("JobTitle", "")
        if not title_matches(title):
            continue
        jid = j.get("JobId")
        url = f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}"
        content = ""
        try:
            rd = requests.get(url, headers=UA, timeout=20)
            if rd.status_code == 200:
                i = rd.text.find("Description</div>")
                if i >= 0:
                    seg = rd.text[i + len("Description</div>"):]
                    end = seg.find('id="removeElementsOnPrint"')
                    content = strip_html(seg[:end if 0 < end < 40000 else 40000])
        except requests.RequestException:
            pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        jl = j.get("JobLocation") or {}
        loc = ", ".join(str(x) for x in
                        (jl.get("City"), jl.get("State"), jl.get("Country")) if x)
        pay_min, pay_max, pay_text = extract_pay(content)
        wt = "Remote" if j.get("IsRemote") else ""
        out.append(Job(
            company=slug, ats="paylocity", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at=(j.get("PublishedDate") or "")[:10], updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, wt),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


_DAYFORCE_LOCK = threading.Lock()
_DAYFORCE = {}


def _dayforce_session():
    """Shared session + CSRF token (jobs.dayforcehcm.com 403s without them)."""
    with _DAYFORCE_LOCK:
        if "sess" not in _DAYFORCE:
            s = requests.Session()
            s.headers.update(UA)
            s.get("https://jobs.dayforcehcm.com/en-US/gannett/candidateportal",
                  timeout=20)
            tok = s.get("https://jobs.dayforcehcm.com/api/auth/csrf",
                        timeout=20).json()["csrfToken"]
            _DAYFORCE["sess"], _DAYFORCE["tok"] = s, tok
        return _DAYFORCE["sess"], _DAYFORCE["tok"]


def fetch_dayforce(config):
    """
    Dayforce HCM job boards. config: {"slug": "acme", "client": "acme",
    "site": "CANDIDATEPORTAL"}. POST /api/geo/<client>/jobposting/search
    (session + x-csrf-token) returns 25/page with the full description
    inline and exact isoCountryCode per location.
    """
    slug, client, site = config["slug"], config["client"], config["site"]
    out = []
    try:
        s, tok = _dayforce_session()
        start = 0
        while start < 300:
            r = s.post(f"https://jobs.dayforcehcm.com/api/geo/{client}/jobposting/search",
                       json={"clientNamespace": client, "jobBoardCode": site,
                             "cultureCode": "en-US", "paginationStart": start},
                       headers={"Content-Type": "application/json",
                                "x-csrf-token": tok}, timeout=20)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                break
            d = r.json()
            jobs = d.get("jobPostings") or []
            if not jobs:
                break
            for j in jobs:
                title = j.get("jobTitle", "")
                if not title_matches(title):
                    continue
                content = strip_html(j.get("jobDescription") or "")
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                plocs = j.get("postingLocations") or []
                loc = "; ".join(p.get("formattedAddress") or "" for p in plocs if p)
                pay_min, pay_max, pay_text = extract_pay(content)
                wt = "Remote" if j.get("hasVirtualLocation") else ""
                out.append(Job(
                    company=slug, ats="dayforce", title=title, location=loc,
                    url=(f"https://jobs.dayforcehcm.com/en-US/{client}/{site}"
                         f"/jobs/{j.get('jobPostingId')}"),
                    ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet, description=content,
                    posted_at=(j.get("postingStartTimestampUTC") or "")[:10],
                    updated_at="",
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content, wt),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            start += len(jobs)
            if start >= (d.get("maxCount") or 0):
                break
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        pass
    return out


def fetch_recruitee(slug):
    """
    Recruitee: GET <slug>.recruitee.com/api/offers/ has everything inline
    (description, exact country_code, salary dict, remote/hybrid flags).
    """
    try:
        r = requests.get(f"https://{slug}.recruitee.com/api/offers/",
                         headers=UA, timeout=15)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return []
        offers = (r.json() or {}).get("offers") or []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for o in offers:
        title = o.get("title", "")
        if not title_matches(title):
            continue
        content = strip_html((o.get("description") or "") + " " +
                             (o.get("requirements") or ""))
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = o.get("location") or ", ".join(
            x for x in (o.get("city"), o.get("country")) if x)
        pay_min, pay_max, pay_text = extract_pay(content)
        sal = o.get("salary") or {}
        if not pay_max and isinstance(sal, dict):
            try:
                mn, mx = int(sal.get("min") or 0), int(sal.get("max") or 0)
                if mx > 10000:
                    pay_min, pay_max = mn, mx
                    pay_text = f"${mn:,} - ${mx:,}"
            except (TypeError, ValueError):
                pass
        wt = "Remote" if o.get("remote") else ("Hybrid" if o.get("hybrid") else "")
        out.append(Job(
            company=slug, ats="recruitee", title=title, location=loc,
            url=o.get("careers_url") or f"https://{slug}.recruitee.com/o/{o.get('slug','')}",
            ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at=(o.get("published_at") or "")[:10],
            updated_at=(o.get("updated_at") or "")[:10],
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, wt),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


GEM_LIST_QUERY = """query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings { id extId title locations { name city isoCountry isRemote } }
  }
}"""
GEM_DETAIL_QUERY = """query ExternalJobPostingQuery($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id title descriptionHtml startDateTs compensationHtml
  }
}"""


def _gem_gql(op, query, variables):
    r = requests.post("https://jobs.gem.com/api/public/graphql/batch",
                      json=[{"operationName": op, "query": query,
                             "variables": variables}],
                      headers={**UA, "Content-Type": "application/json"},
                      timeout=20)
    if r.status_code != 200 or not r.text.strip().startswith("["):
        return {}
    return (r.json() or [{}])[0].get("data") or {}


def fetch_gem(slug):
    """
    Gem job boards (jobs.gem.com) — public GraphQL. JobBoardList gives the
    postings with exact isoCountry; ExternalJobPostingQuery per matched job
    returns descriptionHtml + compensationHtml.
    """
    out = []
    try:
        d = _gem_gql("JobBoardList", GEM_LIST_QUERY, {"boardId": slug})
    except (requests.RequestException, json.JSONDecodeError):
        return []
    posts = ((d.get("oatsExternalJobPostings") or {}).get("jobPostings")) or []
    for p in posts:
        title = p.get("title", "")
        if not title_matches(title):
            continue
        ext = p.get("extId", "")
        content = ""
        comp = ""
        posted = ""
        try:
            dd = _gem_gql("ExternalJobPostingQuery", GEM_DETAIL_QUERY,
                          {"boardId": slug, "extId": ext})
            post = dd.get("oatsExternalJobPosting") or {}
            content = strip_html(post.get("descriptionHtml") or "")
            comp = strip_html(post.get("compensationHtml") or "")
            ts = post.get("startDateTs")
            if ts:
                posted = time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        names = [L.get("name") or L.get("city") or "" for L in (p.get("locations") or [])]
        loc = "; ".join(x for x in names if x)
        remote_flag = any(L.get("isRemote") for L in (p.get("locations") or []))
        pay_min, pay_max, pay_text = extract_pay(content + " " + comp)
        out.append(Job(
            company=slug, ats="gem", title=title, location=loc,
            url=f"https://jobs.gem.com/{slug}/{ext}",
            ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at=posted, updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, "Remote" if remote_flag else ""),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


COMEET_DATA_RE = re.compile(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);", re.S)


def fetch_comeet(config):
    """
    Comeet hosted boards (www.comeet.com/jobs/<company>/<uid>). The board
    page embeds COMPANY_POSITIONS_DATA (location.country is an exact ISO
    code); each position's hosted page embeds the description JSON string.
    """
    slug, uid = config["slug"], config["uid"]
    try:
        r = requests.get(f"https://www.comeet.com/jobs/{slug}/{uid}",
                         headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        m = COMEET_DATA_RE.search(r.text)
        if not m:
            return []
        jobs = json.loads(m.group(1))
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in jobs:
        title = j.get("name", "")
        if not title_matches(title):
            continue
        url = j.get("url_comeet_hosted_page") or j.get("url_active_page") or ""
        content = ""
        if url:
            try:
                rd = requests.get(url, headers=UA, timeout=20)
                if rd.status_code == 200:
                    m2 = JSON_STR_RE.search(rd.text)
                    if m2:
                        content = strip_html(_json_str(m2))
            except requests.RequestException:
                pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc_obj = j.get("location") or {}
        loc = ", ".join(str(x) for x in
                        (loc_obj.get("city") or loc_obj.get("name"),
                         loc_obj.get("state"), loc_obj.get("country")) if x)
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="comeet", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at=(j.get("time_updated") or "")[:10], updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, j.get("workplace_type") or ""),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


def fetch_jobscore(slug):
    """
    Jobscore boards (careers.jobscore.com/careers/<slug>) are server-rendered
    HTML; detail pages carry a JSON-LD JobPosting with the description.
    """
    try:
        r = requests.get(f"https://careers.jobscore.com/careers/{slug}",
                         headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        txt = r.text
    except requests.RequestException:
        return []
    out = []
    seen = set()
    for m in re.finditer(
            r'href="(/careers/%s/jobs/[^"#?]+)[^"]*"[^>]*>\s*([^<]+?)\s*</a>'
            % re.escape(slug), txt):
        path, title = m.group(1), html.unescape(m.group(2)).strip()
        if path in seen or not title:
            continue
        seen.add(path)
        if not title_matches(title):
            continue
        url = "https://careers.jobscore.com" + path
        content, loc, posted = "", "", ""
        try:
            rd = requests.get(url, headers=UA, timeout=20)
            if rd.status_code == 200:
                ld = _ld_jobposting(rd.text)
                content = strip_html(ld.get("description", ""))
                posted = (ld.get("datePosted") or "")[:10]
                jl = ld.get("jobLocation") or []
                jl = jl if isinstance(jl, list) else [jl]
                parts = []
                for a in jl:
                    ad = (a or {}).get("address") or {}
                    parts.append(", ".join(str(x) for x in
                                           (ad.get("addressLocality"),
                                            ad.get("addressRegion"),
                                            ad.get("addressCountry")) if x))
                loc = "; ".join(p for p in parts if p)
        except requests.RequestException:
            pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="jobscore", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at=posted, updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


def fetch_trakstar(slug):
    """
    Trakstar Hire (ex-Recruiterbox): jsapi.recruiterbox.com/v1/openings
    ?client_name=<slug> returns everything inline (description, hosted_url,
    location with full country names).
    """
    out = []
    try:
        r = requests.get("https://jsapi.recruiterbox.com/v1/openings",
                         params={"client_name": slug, "limit": 200},
                         headers=UA, timeout=15)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return []
        jobs = (r.json() or {}).get("objects") or []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    for j in jobs:
        title = j.get("title", "")
        if not title_matches(title):
            continue
        content = strip_html(j.get("description") or "")
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc_obj = j.get("location") or {}
        loc = ", ".join(str(x) for x in (loc_obj.get("city"), loc_obj.get("state"),
                                         loc_obj.get("country")) if x)
        pay_min, pay_max, pay_text = extract_pay(content)
        wt = "Remote" if j.get("allows_remote") else ""
        out.append(Job(
            company=slug, ats="trakstar", title=title, location=loc,
            url=j.get("hosted_url") or f"https://{slug}.hire.trakstar.com/jobs/{j.get('id','')}",
            ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at="", updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, wt),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


def fetch_applicantpro(slug):
    """
    ApplicantPro (isolved Talent Acquisition). The /jobs/ page embeds a
    domainId; /core/jobs/<domainId> returns the list JSON (exact iso3
    country). Job pages carry a JSON-LD JobPosting with the description.
    Dead boards redirect to notset.php.
    """
    try:
        r = requests.get(f"https://{slug}.applicantpro.com/jobs/",
                         headers=UA, timeout=20, allow_redirects=True)
        if (r.status_code != 200 or "applicantpro.com" not in r.url
                or "notset.php" in r.url):
            return []
        ids = re.findall(r"domainId[\"']?\s*[:=]\s*[\"']?(\d+)", r.text) or \
            [i for i in re.findall(r"domain_id[\"']?\s*[:=]\s*[\"']?(\d+)", r.text)
             if len(i) > 2]
        if not ids:
            return []
        r2 = requests.get(f"https://{slug}.applicantpro.com/core/jobs/{ids[0]}"
                          "?getParams=%7B%7D", headers=UA, timeout=20)
        if r2.status_code != 200 or not r2.text.strip().startswith("{"):
            return []
        jobs = ((r2.json() or {}).get("data") or {}).get("jobs") or []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in jobs:
        title = j.get("title", "")
        if not title_matches(title):
            continue
        url = j.get("jobUrl") or f"https://{slug}.applicantpro.com/jobs/{j.get('id','')}"
        content = ""
        try:
            rd = requests.get(url, headers=UA, timeout=20)
            if rd.status_code == 200:
                content = strip_html(_ld_jobposting(rd.text).get("description", ""))
        except requests.RequestException:
            pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = ", ".join(str(x) for x in
                        (j.get("city"), j.get("abbreviation"), j.get("iso3")) if x)
        pay_min, pay_max, pay_text = extract_pay(
            content + " " + str(j.get("payDetails") or ""))
        if not pay_max:
            try:
                mn = int(float(j.get("minSalary") or 0))
                mx = int(float(j.get("maxSalary") or 0))
                if mx > 10000:
                    pay_min, pay_max = mn, mx
                    pay_text = f"${mn:,} - ${mx:,}"
            except (TypeError, ValueError):
                pass
        out.append(Job(
            company=slug, ats="applicantpro", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at="", updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, j.get("workplaceType") or ""),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


def fetch_pinpoint(slug):
    """
    Pinpoint: GET <slug>.pinpointhq.com/postings.json → data[] with
    description/key_responsibilities HTML, compensation min/max, url,
    workplace_type_text. No country field — location has city/province.
    """
    try:
        r = requests.get(f"https://{slug}.pinpointhq.com/postings.json",
                         headers=UA, timeout=15)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return []
        jobs = (r.json() or {}).get("data") or []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in jobs:
        title = j.get("title", "")
        if not title_matches(title):
            continue
        content = strip_html(" ".join(
            str(j.get(k) or "") for k in
            ("description", "key_responsibilities", "skills_knowledge_expertise")))
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc_obj = j.get("location") or {}
        if not isinstance(loc_obj, dict):
            loc_obj = {"name": str(loc_obj)}
        loc = ", ".join(str(x) for x in
                        (loc_obj.get("city"), loc_obj.get("province")) if x) \
            or (loc_obj.get("name") or "")
        pay_min, pay_max, pay_text = extract_pay(content)
        if not pay_max:
            try:
                mn = int(float(j.get("compensation_minimum") or 0))
                mx = int(float(j.get("compensation_maximum") or 0))
                if mx > 10000 and (j.get("compensation_frequency") or "").lower() not in ("hourly",):
                    pay_min, pay_max = mn, mx
                    pay_text = f"${mn:,} - ${mx:,}"
            except (TypeError, ValueError):
                pass
        out.append(Job(
            company=slug, ats="pinpoint", title=title, location=loc,
            url=j.get("url") or f"https://{slug}.pinpointhq.com",
            ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at="", updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, j.get("workplace_type_text") or ""),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


ADP_STAFFING_BASE = ("https://workforcenow.adp.com/mascsr/default/careercenter/"
                     "public/events/staffing/v1/job-requisitions")


def fetch_adp(config):
    """
    ADP Workforce Now career center. config: {"slug": "...", "cid": "<guid>"}
    GET job-requisitions?cid=<guid> lists reqs (paged via $skip; ~20/page);
    each requisitionLocations[].nameCode.shortName is a "City, ST, US" string.
    The per-item detail (/job-requisitions/<itemID>) carries the full
    requisitionDescription HTML.
    """
    cid = config["cid"]
    slug = config["slug"]
    q = f"cid={cid}&lang=en_US&ccId=19000101_000001&locale=en_US"
    out = []
    skip = 0
    try:
        while skip < 400:
            r = requests.get(f"{ADP_STAFFING_BASE}?{q}&$top=20&$skip={skip}",
                             headers=UA, timeout=20)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                break
            d = r.json()
            reqs = d.get("jobRequisitions") or []
            if not reqs:
                break
            total = (d.get("meta") or {}).get("totalNumber") or len(reqs)
            for j in reqs:
                title = j.get("requisitionTitle", "")
                if not title_matches(title):
                    continue
                iid = j.get("itemID", "")
                parts = [((L.get("nameCode") or {}).get("shortName") or "").strip()
                         for L in (j.get("requisitionLocations") or [])]
                loc = "; ".join(p for p in parts if p)
                url = (f"https://workforcenow.adp.com/mascsr/default/careercenter/"
                       f"public/events/staffing/v1/job-requisitions/{iid}?{q}")
                content = ""
                for _ in range(3):
                    try:
                        rd = requests.get(url, headers=UA, timeout=20)
                        if rd.status_code == 200 and rd.text.strip().startswith("{"):
                            content = strip_html((rd.json() or {}).get(
                                "requisitionDescription") or "")
                            break
                        if rd.status_code not in (429, 500, 502, 503):
                            break
                    except (requests.RequestException, json.JSONDecodeError):
                        pass
                    time.sleep(1.5)
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                pay_min, pay_max, pay_text = extract_pay(content)
                out.append(Job(
                    company=slug, ats="adp", title=title, location=loc,
                    url=(f"https://workforcenow.adp.com/mascsr/default/careercenter/"
                         f"public/events/globalsearch/index.html?cid={cid}"),
                    ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet, description=content,
                    posted_at=(j.get("postDate") or "")[:10], updated_at="",
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            skip += len(reqs)
            if skip >= total:
                break
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return out


PAYCOM_SEARCH_BODY = {"skip": 0, "take": 100, "filtersForQuery": {
    "distanceFrom": 0, "workEnvironments": [], "positionTypes": [],
    "educationLevels": [], "categories": [], "travelTypes": [],
    "shiftTypes": [], "otherFilters": [], "keywordSearchText": "",
    "location": "", "sortOption": ""}}


def fetch_paycom(config):
    """
    Paycom career portal. config: {"slug": "...", "clientkey": "<hex>"}
    GET web.php/api/portal-session-jwt?clientkey=<K> yields a session JWT;
    POST job-posting-previews/search (Authorization: <JWT>) to the regional
    mantle host returns each preview's full description + "City, ST" locations.
    """
    key = config["clientkey"]
    slug = config["slug"]
    try:
        r = requests.get("https://www.paycomonline.net/v4/ats/web.php/api/"
                         f"portal-session-jwt?clientkey={key}", headers=UA, timeout=20)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return []
        tok = (r.json() or {}).get("sessionJWT") or ""
        if not tok:
            return []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    region = config.get("region", "us-cent")
    def search(reg):
        return requests.post(
            f"https://portal-applicant-tracking.{reg}.paycomonline.net"
            "/api/ats/job-posting-previews/search", json=PAYCOM_SEARCH_BODY,
            headers={**UA, "Content-Type": "application/json",
                     "Authorization": tok}, timeout=20)
    try:
        r2 = search(region)
        if r2.status_code != 200 or not r2.text.strip().startswith("{"):
            rc = requests.get("https://www.paycomonline.net/v4/ats/web.php/portal/"
                              f"{key}/career-page", headers=UA, timeout=20)
            m = re.search(r"portal-applicant-tracking\.([a-z0-9-]+)\.paycomonline",
                          rc.text)
            if not m:
                return []
            region = m.group(1)
            r2 = search(region)
            if r2.status_code != 200 or not r2.text.strip().startswith("{"):
                return []
        jobs = (r2.json() or {}).get("jobPostingPreviews") or []
    except (requests.RequestException, json.JSONDecodeError):
        return []
    out = []
    for j in jobs:
        title = j.get("jobTitle", "")
        if not title_matches(title):
            continue
        content = strip_html(j.get("description") or "")
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        loc = str(j.get("locations") or "").strip()
        jid = j.get("jobId", "")
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="paycom", title=title, location=loc,
            url=(f"https://www.paycomonline.net/v4/ats/web.php/jobs/"
                 f"ViewJobDetails?job={jid}&clientkey={key}"),
            ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at="", updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content, str(j.get("remoteType") or "")),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


CSOD_TOKEN_RE = re.compile(r'"token":"([A-Za-z0-9._-]+)"')
CSOD_CTX_RE = re.compile(r"csod\.context\s*=\s*(\{.*?\});", re.S)


def fetch_csod(config):
    """
    Cornerstone (csod.com) career site. config:
      {"slug": "...", "host": "<sub>", "site": "<careersite id>"}
    The /ux/ats/careersite/<id>/home page embeds a bearer JWT + culture; POST
    /services/x/career-site/v1/search returns requisitions with structured
    locations. Detail pages carry the description behind the same bearer.
    """
    host, sid = config["host"], config["site"]
    try:
        r = requests.get(f"https://{host}.csod.com/ux/ats/careersite/{sid}/home",
                         headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        tm = CSOD_TOKEN_RE.search(r.text)
        if not tm:
            return []
        tok = tm.group(1)
        culture_id, culture_name = 1, "en-US"
        cm = CSOD_CTX_RE.search(r.text)
        if cm:
            try:
                ctx = json.loads(cm.group(1))
                culture_id = ctx.get("cultureID") or 1
                culture_name = ctx.get("cultureName") or "en-US"
            except json.JSONDecodeError:
                pass
    except requests.RequestException:
        return []
    slug = config["slug"]
    auth = {**UA, "Content-Type": "application/json", "Authorization": f"Bearer {tok}"}
    out = []
    page = 1
    try:
        while page <= 10:
            body = {"careerSiteId": int(sid), "careerSitePageId": int(sid),
                    "pageNumber": page, "pageSize": 100, "cultureId": culture_id,
                    "searchText": "", "cultureName": culture_name, "states": [],
                    "countryCodes": [], "cities": [], "placeID": "", "radius": None,
                    "postingsWithinDays": None, "customFieldCheckboxKeys": [],
                    "customFieldDropdowns": [], "customFieldRadios": []}
            rs = requests.post(f"https://{host}.csod.com/services/x/career-site/v1/search",
                               json=body, headers=auth, timeout=20)
            if rs.status_code != 200 or not rs.text.strip().startswith("{"):
                break
            data = (rs.json() or {}).get("data") or {}
            jobs = data.get("requisitions") or []
            if not jobs:
                break
            for j in jobs:
                title = j.get("displayJobTitle", "")
                if not title_matches(title):
                    continue
                rid = j.get("requisitionId") or j.get("id") or ""
                parts = [", ".join(str(x) for x in (L.get("city"), L.get("state"),
                                                    L.get("country")) if x)
                         for L in (j.get("locations") or [])]
                loc = "; ".join(p for p in parts if p)
                url = f"https://{host}.csod.com/ux/ats/careersite/{sid}/job/{rid}"
                content = ""
                try:
                    rd = requests.get(f"https://{host}.csod.com/services/x/"
                                      f"job-requisition/v2/requisitions/{rid}/"
                                      f"jobDetails?cultureId={culture_id}",
                                      headers=auth, timeout=20)
                    if rd.status_code == 200 and rd.text.strip().startswith("{"):
                        rdata = (rd.json() or {}).get("data") or {}
                        content = strip_html(rdata.get("externalDescription")
                                             or rdata.get("jobDescription")
                                             or rdata.get("description") or "")
                except (requests.RequestException, json.JSONDecodeError):
                    pass
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                pay_min, pay_max, pay_text = extract_pay(content)
                out.append(Job(
                    company=slug, ats="csod", title=title, location=loc,
                    url=url, ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet, description=content,
                    posted_at="", updated_at="",
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            if len(jobs) < 100:
                break
            page += 1
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return out


def fetch_eightfold(config):
    """
    Eightfold career hub. config: {"slug": "...", "domain": "acme.com"}
    GET app.eightfold.ai/api/apply/v2/jobs?domain=<d> lists positions with a
    full "City, Zip, United States" location; the per-position detail carries
    job_description. Custom-domain tenants answer "Not authorized for PCSX".
    """
    domain = config["domain"]
    slug = config["slug"]
    out = []
    start = 0
    try:
        while start < 500:
            r = requests.get("https://app.eightfold.ai/api/apply/v2/jobs"
                             f"?domain={domain}&num=100&start={start}",
                             headers=UA, timeout=20)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                break
            d = r.json()
            ps = d.get("positions") or []
            if not ps:
                break
            for p in ps:
                title = p.get("name", "")
                if not title_matches(title):
                    continue
                ls = p.get("locations") or ([p.get("location")] if p.get("location") else [])
                loc = "; ".join(str(x) for x in ls[:3] if x)
                content = strip_html(p.get("job_description") or "")
                pid = p.get("id") or p.get("position_id") or ""
                if not content and pid:
                    try:
                        rd = requests.get("https://app.eightfold.ai/api/apply/v2/"
                                          f"positions/{pid}?domain={domain}",
                                          headers=UA, timeout=20)
                        if rd.status_code == 200 and rd.text.strip().startswith("{"):
                            content = strip_html((rd.json() or {}).get(
                                "job_description") or "")
                    except (requests.RequestException, json.JSONDecodeError):
                        pass
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                pay_min, pay_max, pay_text = extract_pay(content)
                out.append(Job(
                    company=slug, ats="eightfold", title=title, location=loc,
                    url=f"https://app.eightfold.ai/careers/job/{pid}?domain={domain}",
                    ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet, description=content,
                    posted_at="", updated_at="",
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content,
                                           str(p.get("work_location_option") or "")),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            start += len(ps)
            if start >= (d.get("count") or 0):
                break
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return out


AVATURE_TITLE_RE = re.compile(
    r'<a[^>]*href="([^"]*JobDetail/[^"]*)"[^>]*>\s*([^<]{2,120}?)\s*</a>')


def fetch_avature(slug):
    """
    Avature-hosted career sites (<slug>.avature.net). Server-rendered; job
    titles live in JobDetail anchors, detail pages carry a JSON-LD JobPosting
    with description + address.
    """
    txt = None
    base = f"https://{slug}.avature.net"
    for path in ("careers/SearchJobs", "careers", "en_US/careers/SearchJobs"):
        try:
            r = requests.get(f"{base}/{path}", headers=UA, timeout=20)
        except requests.RequestException:
            continue
        if r.status_code == 200 and AVATURE_TITLE_RE.search(r.text):
            txt = r.text
            break
    if txt is None:
        return []
    out = []
    seen = set()
    for m in AVATURE_TITLE_RE.finditer(txt):
        href, title = m.group(1), html.unescape(re.sub(r"\s+", " ", m.group(2))).strip()
        if href in seen or not title:
            continue
        seen.add(href)
        if not title_matches(title):
            continue
        url = href if href.startswith("http") else base + ("" if href.startswith("/") else "/") + href
        content, loc, posted = "", "", ""
        try:
            rd = requests.get(url, headers=UA, timeout=20)
            if rd.status_code == 200:
                ld = _ld_jobposting(rd.text)
                content = strip_html(ld.get("description", ""))
                posted = (ld.get("datePosted") or "")[:10]
                jl = ld.get("jobLocation") or []
                jl = jl if isinstance(jl, list) else [jl]
                parts = []
                for a in jl:
                    ad = (a or {}).get("address") or {}
                    parts.append(", ".join(str(x) for x in
                                           (ad.get("addressLocality"),
                                            ad.get("addressRegion"),
                                            ad.get("addressCountry")) if x))
                loc = "; ".join(p for p in parts if p)
        except requests.RequestException:
            pass
        hits, snippet = find_ai_hits(content)
        if not hits and REQUIRE_AI_HITS:
            continue
        if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
            continue
        pay_min, pay_max, pay_text = extract_pay(content)
        out.append(Job(
            company=slug, ats="avature", title=title, location=loc,
            url=url, ai_hits=hits,
            ai_hit_count=sum(content.lower().count(h) for h in hits),
            snippet=snippet, description=content,
            posted_at=posted, updated_at="",
            pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
            remote=classify_remote(loc, content),
            stage=get_meta(slug)[0], sector=get_meta(slug)[1],
        ))
    return out


BRASSRING_SEARCH_URL = ("https://sjobs.brassring.com/TGnewUI/Search/Ajax/"
                        "ProcessSortAndShowMoreJobs")


def fetch_brassring(config):
    """
    Kenexa/IBM BrassRing (sjobs.brassring.com). config:
      {"slug": "...", "partnerid": "<pid>", "siteid": "<sid>"}
    Session GET the TGnewUI home, then POST ProcessSortAndShowMoreJobs; each
    Job carries a Questions list (jobtitle / location / description as
    key-value pairs).
    """
    pid, sid = config["partnerid"], config["siteid"]
    slug = config["slug"]
    s = requests.Session()
    s.headers.update(UA)
    try:
        s.get(f"https://sjobs.brassring.com/TGnewUI/Search/Home/Home"
              f"?partnerid={pid}&siteid={sid}", timeout=20)
    except requests.RequestException:
        pass
    out = []
    page = 1
    try:
        while page <= 10:
            body = {"partnerId": pid, "siteId": sid, "keyword": "", "location": "",
                    "turnOffHttps": False, "facetfilterfields": {"Facet": []},
                    "powersearchoptions": {"PowerSearchOption": []},
                    "SortType": "LastUpdated", "pageNumber": page,
                    "encryptedSessionValue": ""}
            r = s.post(BRASSRING_SEARCH_URL, json=body,
                       headers={"Content-Type": "application/json"}, timeout=20)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                break
            d = r.json()
            jobs = (d.get("Jobs") or {}).get("Job") or []
            if not jobs:
                break
            for j in jobs:
                qs = {q.get("QuestionName", "").lower(): q.get("Value")
                      for q in (j.get("Questions") or [])}
                title = str(qs.get("jobtitle") or qs.get("title") or "")
                if not title_matches(title):
                    continue
                loc = str(qs.get("location") or qs.get("formtext:location") or "")
                content = strip_html(str(qs.get("jobdescription")
                                         or qs.get("description") or ""))
                jid = j.get("JobId") or j.get("Id") or ""
                hits, snippet = find_ai_hits(content)
                if not hits and REQUIRE_AI_HITS:
                    continue
                if REQUIRE_ETL_SIGNAL and not has_etl_signal(slug, title, content):
                    continue
                pay_min, pay_max, pay_text = extract_pay(content)
                out.append(Job(
                    company=slug, ats="brassring", title=title, location=loc,
                    url=(f"https://sjobs.brassring.com/TGnewUI/Search/Home/"
                         f"HomeWithPreLoad?PageType=JobDetails&partnerid={pid}"
                         f"&siteid={sid}&jobId={jid}"),
                    ai_hits=hits,
                    ai_hit_count=sum(content.lower().count(h) for h in hits),
                    snippet=snippet, description=content,
                    posted_at="", updated_at="",
                    pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
                    remote=classify_remote(loc, content),
                    stage=get_meta(slug)[0], sector=get_meta(slug)[1],
                ))
            if len(jobs) < 20:
                break
            page += 1
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return out


def fetch_adp_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_adp(entry)
    return []


def fetch_paycom_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_paycom(entry)
    return []


def fetch_csod_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_csod(entry)
    return []


def fetch_eightfold_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_eightfold(entry)
    return []


def fetch_brassring_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_brassring(entry)
    return []


def fetch_ultipro_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_ultipro(entry)
    return []


def fetch_paylocity_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_paylocity(entry)
    return []


def fetch_dayforce_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_dayforce(entry)
    return []


def fetch_comeet_wrapper(entry):
    if isinstance(entry, dict):
        return fetch_comeet(entry)
    return []


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday_wrapper,
    "accenture": fetch_accenture,
    "workable": fetch_workable,
    "teamtailor": fetch_teamtailor,
    "bamboohr": fetch_bamboohr,
    "breezy": fetch_breezy,
    "jazzhr": fetch_jazzhr,
    "jobvite": fetch_jobvite,
    "rippling": fetch_rippling,
    "smartrecruiters": fetch_smartrecruiters,
    "jibe": fetch_jibe,
    "icims": fetch_icims,
    "ultipro": fetch_ultipro_wrapper,
    "paylocity": fetch_paylocity_wrapper,
    "dayforce": fetch_dayforce_wrapper,
    "recruitee": fetch_recruitee,
    "gem": fetch_gem,
    "comeet": fetch_comeet_wrapper,
    "jobscore": fetch_jobscore,
    "trakstar": fetch_trakstar,
    "applicantpro": fetch_applicantpro,
    "pinpoint": fetch_pinpoint,
    "adp": fetch_adp_wrapper,
    "paycom": fetch_paycom_wrapper,
    "csod": fetch_csod_wrapper,
    "eightfold": fetch_eightfold_wrapper,
    "avature": fetch_avature,
    "brassring": fetch_brassring_wrapper,
}

def _write_progress(path, done, total, jobs_found, phase="scan", **extra):
    # Atomic write so a concurrent reader never sees a partial file.
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_text(json.dumps(
            {"phase": phase, "done": done, "total": total,
             "jobs_found": jobs_found, **extra}))
        tmp.replace(path)
    except OSError:
        pass


def scan(companies, max_workers=24, progress_file=None, accenture_keywords=None):
    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for ats, slugs in companies.items():
            if ats.startswith("_") or ats not in FETCHERS:
                continue
            for slug in slugs:
                if ats == "accenture" and accenture_keywords:
                    for kw in accenture_keywords:
                        tasks.append(ex.submit(fetch_accenture, slug, kw))
                else:
                    tasks.append(ex.submit(FETCHERS[ats], slug))
        total = len(tasks)
        if progress_file:
            _write_progress(progress_file, 0, total, 0)
        results = []
        done = 0
        last_write = 0.0
        for fut in as_completed(tasks):
            try:
                results.extend(fut.result())
            except Exception as e:
                print(f"[warn] task failed: {e}", file=sys.stderr)
            done += 1
            now = time.time()
            if progress_file and (done == total or now - last_write >= 0.5):
                _write_progress(progress_file, done, total, len(results))
                last_write = now
    if REQUIRE_HEALTHTECH_SECTOR:
        results = [j for j in results if j.sector == "healthtech"]
    if REQUIRE_AEROSPACE_SECTOR:
        results = [j for j in results if j.sector == "aerospace"]
    if REQUIRE_REMOTE_OR_LOCAL:
        results = [j for j in results if is_remote_or_local(j, LOCAL_RE)]
    return results


def local_rx(p):
    """Compiled regex for a profile's "local area" (require_remote_or_local).

    Falls back to the active fit profile's location_re, so a tab needs no extra
    config when the resume already says where home is.
    """
    pat = (p or {}).get("local_re") or ""
    if pat.strip():
        try:
            return re.compile(pat, re.I)
        except re.error:
            pass
    return ACTIVE_FIT.get("location_re")


def is_remote_or_local(job, rx):
    if job.remote == "remote":
        return True
    return bool(rx and rx.search(job.location or ""))

# ---------------------------------------------------------------------------
# Superset scan (--all): fetch once with the union of every profile's title
# patterns and no excludes / AI-hit / sector requirements, cache the raw jobs,
# then derive each profile's .latest_<profile>.json locally via apply_profile().
# Greenhouse/Lever/Ashby return descriptions board-level so the union costs
# nothing extra there; walk-style ATSes (Workday etc.) fetch a few more detail
# pages. One scan replaces one full scan per profile.

def set_superset_globals():
    global LEVEL_PATTERNS, DOMAIN_PATTERNS, EXCLUDE_TITLE_PATTERNS
    global REQUIRE_AI_HITS, REQUIRE_ETL_SIGNAL
    global REQUIRE_HEALTHTECH_SECTOR, REQUIRE_AEROSPACE_SECTOR, REQUIRE_REMOTE_OR_LOCAL
    level, domain = [], []
    for p in PROFILES.values():
        for pat in p["level"]:
            if pat not in level:
                level.append(pat)
        for pat in p["domain"]:
            if pat not in domain:
                domain.append(pat)
    LEVEL_PATTERNS = level
    DOMAIN_PATTERNS = domain
    EXCLUDE_TITLE_PATTERNS = []
    REQUIRE_AI_HITS = False
    REQUIRE_ETL_SIGNAL = False
    REQUIRE_HEALTHTECH_SECTOR = False
    REQUIRE_AEROSPACE_SECTOR = False
    REQUIRE_REMOTE_OR_LOCAL = False


def dedup_jobs(jobs):
    # The superset scan can yield the same posting twice (e.g. Accenture once
    # per search keyword).
    seen, out = set(), []
    for j in jobs:
        key = j.url or f"{j.company}|{j.title}|{j.location}"
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


def apply_profile(jobs, key, min_ai_hits=None):
    """Filter a raw superset scan down to one profile. Pure/local — no network."""
    p = PROFILES[key]
    if min_ai_hits is None:
        min_ai_hits = int(p.get("min_ai_hits", 1))
    rx_local = local_rx(p)
    out = []
    for j in jobs:
        if not title_matches_for(j.title, p["level"], p["domain"], p["exclude"]):
            continue
        if len(j.ai_hits) < min_ai_hits:
            continue
        if p.get("require_etl_signal") and not has_etl_signal(
                j.company, j.title, j.description or j.snippet):
            continue
        if p.get("require_healthtech_sector") and j.sector != "healthtech":
            # "health_adjacent_ok" widens the tab from healthtech *companies*
            # to healthcare-facing *postings* anywhere (Job.health == "adj").
            if not (p.get("health_adjacent_ok") and j.health == "adj"):
                continue
        if p.get("require_aerospace_sector") and j.sector != "aerospace":
            continue
        if p.get("require_remote_or_local") and not is_remote_or_local(j, rx_local):
            continue
        out.append(j)
    # A tab may target a different resume than the run's --fit ("fit" key in
    # profiles.json). Re-score in place: the caller serializes each tab right
    # after deriving it, and the next tab re-scores again anyway.
    fp = FIT_PROFILES.get(p.get("fit") or "")
    if fp:
        for j in out:
            j.fit = compute_fit(j, fp)
    return out


def run_all(args):
    """--all / --from-raw entry point: one scan (or a cached one), every table."""
    t0 = time.time()
    progress_file = Path(args.progress_file) if args.progress_file else None
    if args.from_raw:
        if progress_file:
            _write_progress(progress_file, 0, 1, 0, phase="load_raw")
        raw = [Job(**d) for d in json.loads(Path(args.from_raw).read_text())]
        for j in raw:  # re-score: the cache may hold another fit profile's scores
            score_job(j)
        print(f"Loaded {len(raw)} raw jobs from {args.from_raw} "
              f"(fit: {args.fit})")
    else:
        set_superset_globals()
        companies = json.loads(Path(args.companies).read_text())
        raw = scan(companies, max_workers=args.workers,
                   progress_file=progress_file,
                   accenture_keywords=accenture_keywords())
        raw = dedup_jobs(raw)
        last_write = 0.0
        for i, j in enumerate(raw):
            score_job(j)
            now = time.time()
            if progress_file and now - last_write >= 0.5:
                _write_progress(progress_file, i + 1, len(raw), len(raw),
                                phase="fit")
                last_write = now
        if progress_file:
            _write_progress(progress_file, 0, 1, len(raw), phase="save_raw")
        Path(args.raw_out).write_text(
            json.dumps([asdict(j) for j in raw]))
        print(f"Raw scan: {len(raw)} jobs -> {args.raw_out} "
              f"in {time.time()-t0:.1f}s")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT
    # Profiles fed by an external crawler (linkedin_scrape.py) own their own
    # .latest_<key>.json — deriving one here from the ATS scan would clobber it.
    profiles = [k for k, v in PROFILES.items() if profile_source(v) == "ats"]
    for i, key in enumerate(profiles):
        if progress_file:
            _write_progress(progress_file, i, len(profiles), len(raw),
                            phase="derive", profile=key)
        jobs = apply_profile(raw, key, args.min_ai_hits)
        out = out_dir / f".latest_{key}.json"
        out.write_text(json.dumps([asdict(j) for j in jobs], indent=2))
        print(f"  {key}: {len(jobs)} jobs -> {out.name}")
    if progress_file:
        _write_progress(progress_file, len(profiles), len(profiles), len(raw),
                        phase="derive")


def print_human(jobs):
    if not jobs:
        print("No matching jobs found. Try lowering --min-ai-hits or adding more company slugs.")
        return
    jobs.sort(key=lambda j: (-j.ai_hit_count, j.company))
    print(f"\nFound {len(jobs)} Director-level role(s) with AI signal:\n")
    for j in jobs:
        print(f"  [{j.company} / {j.ats}] {j.title}")
        print(f"    Location: {j.location}")
        print(f"    AI hits ({j.ai_hit_count}): {', '.join(j.ai_hits[:6])}{'...' if len(j.ai_hits) > 6 else ''}")
        print(f"    Snippet: {j.snippet[:240]}")
        print(f"    URL: {j.url}\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", default=str(COMPANIES_FILE))
    ap.add_argument("--json", help="write results to this JSON file")
    ap.add_argument("--min-ai-hits", type=int, default=None,
                    help="minimum unique AI keywords required (overrides the tab's own setting)")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--progress-file",
                    help="write {done, total, jobs_found} JSON here as companies finish")
    ap.add_argument("--profile", choices=list(PROFILES.keys()),
                    default=next(iter(PROFILES), None),
                    help="role profile (a key in profiles.json; default: the first one)")
    ap.add_argument("--fit", choices=list(FIT_PROFILES.keys()), default=DEFAULT_FIT,
                    help="resume fit profile used for the 0-100 Fit score")
    ap.add_argument("--all", action="store_true",
                    help="one superset scan, then derive every profile's "
                         ".latest_<profile>.json locally")
    ap.add_argument("--raw-out", default=str(ROOT / ".raw_scan.json"),
                    help="where --all caches the raw superset scan")
    ap.add_argument("--from-raw", nargs="?", const=str(ROOT / ".raw_scan.json"),
                    help="skip the network: derive every profile's table from a "
                         "cached raw scan (optionally give the cache path)")
    ap.add_argument("--out-dir",
                    help="where --all/--from-raw write .latest_<profile>.json "
                         "(default: repo dir)")
    args = ap.parse_args()

    set_fit_profile(args.fit)
    if not PROFILES:
        sys.exit("No role profiles configured yet — open the dashboard and pick a "
                 "starter template at /tabs (writes profiles.json).")
    if args.all or args.from_raw:
        run_all(args)
        return

    global LEVEL_PATTERNS, DOMAIN_PATTERNS, EXCLUDE_TITLE_PATTERNS, REQUIRE_ETL_SIGNAL
    global REQUIRE_HEALTHTECH_SECTOR, REQUIRE_AEROSPACE_SECTOR, REQUIRE_REMOTE_OR_LOCAL
    global REQUIRE_AI_HITS, LOCAL_RE
    p = PROFILES[args.profile]
    LEVEL_PATTERNS = p["level"]
    DOMAIN_PATTERNS = p["domain"]
    EXCLUDE_TITLE_PATTERNS = p["exclude"]
    REQUIRE_ETL_SIGNAL = p.get("require_etl_signal", False)
    REQUIRE_HEALTHTECH_SECTOR = p.get("require_healthtech_sector", False)
    REQUIRE_AEROSPACE_SECTOR = p.get("require_aerospace_sector", False)
    REQUIRE_REMOTE_OR_LOCAL = p.get("require_remote_or_local", False)
    LOCAL_RE = local_rx(p)

    global ACCENTURE_KEYWORD
    ACCENTURE_KEYWORD = accenture_keyword(args.profile)

    min_ai_hits = args.min_ai_hits
    if min_ai_hits is None:
        min_ai_hits = int(p.get("min_ai_hits", 1))
    # Let title-matched roles through without an AI keyword when the profile
    # asks for zero hits (aerospace). Fetchers read this global during scan().
    REQUIRE_AI_HITS = min_ai_hits > 0

    companies = json.loads(Path(args.companies).read_text())
    t0 = time.time()
    progress_file = Path(args.progress_file) if args.progress_file else None
    jobs = scan(companies, max_workers=args.workers, progress_file=progress_file)
    jobs = [j for j in jobs if len(j.ai_hits) >= min_ai_hits]
    for j in jobs:
        score_job(j)

    print_human(jobs)
    print(f"Scanned in {time.time()-t0:.1f}s")

    if args.json:
        Path(args.json).write_text(json.dumps([asdict(j) for j in jobs], indent=2))
        print(f"Wrote {len(jobs)} jobs to {args.json}")

if __name__ == "__main__":
    main()
