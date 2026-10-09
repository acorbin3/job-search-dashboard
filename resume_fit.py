"""Turn an uploaded resume into a fit profile (see fit_profiles.py).

Two derivation paths:

* `derive_heuristic(text)` — no network. Counts which of a fixed taxonomy of
  skill groups the resume talks about, picks the top groups as the scoring
  domains (only the terms the resume actually mentions make it into the
  regex), infers the current level from title words, maps groups to company
  sectors, and pulls a home location off the header.
* `derive_with_claude(text)` — asks Claude for the same structure via a JSON
  schema, then compiles it through the same regex builder so the output is
  validated the same way. Used when the `anthropic` package is installed and
  credentials resolve (ANTHROPIC_API_KEY or `ant auth login`); falls back to
  the heuristic otherwise. The resume text leaves the machine only on this path.

`extract_text()` handles .pdf (pypdf, else the `pdftotext` CLI), .docx (stdlib
zipfile), .tex (LaTeX stripped), and plain .txt/.md.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from html import unescape
from io import BytesIO
from pathlib import Path

MAX_RESUME_CHARS = 60_000
CLAUDE_MODEL = os.environ.get("RESUME_FIT_MODEL", "claude-opus-5")

# --------------------------------------------------------------------------
# Text extraction

def extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext == ".pdf":
        text = _pdf_text(data)
    elif ext == ".docx":
        text = _docx_text(data)
    elif ext == ".tex":
        text = _tex_text(data.decode("utf-8", "replace"))
    elif ext in (".txt", ".md", ".markdown", ""):
        text = data.decode("utf-8", "replace")
    else:
        raise ValueError(f"unsupported resume type {ext!r} — use PDF, DOCX, TXT, MD or TEX")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 200:
        raise ValueError("could not read enough text from that file "
                         "(scanned image PDF? try exporting as text)")
    return text[:MAX_RESUME_CHARS]


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # optional dependency
        reader = PdfReader(BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except ImportError:
        pass
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data,
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace")
    raise ValueError("PDF reading needs `pip install pypdf` (or the pdftotext CLI)")


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", " ", xml)
    return unescape(re.sub(r"<[^>]+>", "", xml))


def _tex_text(src: str) -> str:
    src = re.sub(r"(?<!\\)%.*", "", src)                      # comments
    src = re.sub(r"\\(begin|end)\{[^}]*\}", "\n", src)
    src = re.sub(r"\\[a-zA-Z@]+\*?(\[[^\]]*\])?", " ", src)   # \commands[opt]
    src = src.replace("{", " ").replace("}", " ").replace("~", " ")
    return re.sub(r"\\([&%$#_])", r"\1", src)


# --------------------------------------------------------------------------
# Taxonomy

# Each group: display name, the terms we look for (regex fragments, matched
# case-insensitively with word boundaries), and which company sectors a strong
# showing should reward.
GROUPS = [
    ("data-infra", [r"etl", r"elt", r"data pipelines?", r"data integration", r"cdc", r"change data capture",
                    r"connectors?", r"data platform", r"data infrastructure", r"data warehouse", r"snowflake",
                    r"databricks", r"bigquery", r"redshift", r"kafka", r"spark", r"airflow", r"dbt", r"flink",
                    r"data engineering", r"data lake", r"iceberg", r"data quality", r"data governance"],
     {"data-infra": 8, "devtools": 2}),
    ("healthcare", [r"ehr", r"emr", r"epic", r"cerner", r"clinical", r"healthcare", r"health care", r"hipaa",
                    r"fhir", r"hl7", r"patients?", r"medical", r"hospital", r"health system", r"payer", r"provider",
                    r"telehealth", r"pharma", r"life sciences", r"medical imaging", r"radiology", r"dicom"],
     {"healthtech": 8, "medtech": 5, "biotech": 3}),
    ("ml-ai", [r"machine learning", r"deep learning", r"llms?", r"genai", r"generative ai", r"agentic", r"ai agents?",
               r"rag", r"retrieval.augmented", r"pytorch", r"tensorflow", r"nlp", r"computer vision",
               r"model training", r"inference", r"mlops", r"fine.?tun(ed|ing)", r"transformers?", r"embeddings?",
               r"prompt engineering", r"foundation models?", r"scikit", r"xgboost"],
     {"ai-native": 7, "vertical-ai": 5, "data-infra": 2}),
    ("backend", [r"python", r"java", r"go(lang)?", r"kotlin", r"c#", r"\.net", r"node(\.js)?", r"ruby", r"rails",
                 r"django", r"flask", r"fastapi", r"spring", r"rest(ful)? apis?", r"graphql", r"grpc",
                 r"microservices?", r"distributed systems?", r"event.driven", r"rabbitmq", r"redis",
                 r"postgres(ql)?", r"mysql", r"mongodb", r"dynamodb", r"sql"],
     {"devtools": 4, "data-infra": 3}),
    ("frontend", [r"react", r"angular", r"vue", r"typescript", r"javascript", r"next\.js", r"css", r"html",
                  r"front.?end", r"web applications?", r"ui/ux", r"design systems?", r"accessibility", r"redux", r"ngrx"],
     {"productivity": 3, "devtools": 2}),
    ("mobile", [r"ios", r"android", r"swift", r"swiftui", r"objective-c", r"react native", r"flutter", r"mobile apps?"],
     {"productivity": 2}),
    ("cloud-devops", [r"aws", r"gcp", r"google cloud", r"azure", r"kubernetes", r"k8s", r"docker", r"terraform",
                      r"helm", r"ci/cd", r"github actions", r"gitlab", r"jenkins", r"sre", r"observability",
                      r"prometheus", r"grafana", r"datadog", r"infrastructure as code", r"serverless", r"lambda"],
     {"devtools": 5, "data-infra": 2, "security": 1}),
    ("security", [r"security", r"appsec", r"iam", r"oauth", r"soc ?2", r"penetration test(ing)?", r"vulnerabilit(y|ies)",
                  r"zero trust", r"encryption", r"threat model(ing)?", r"compliance", r"siem", r"cissp"],
     {"security": 8}),
    ("fintech", [r"payments?", r"fintech", r"banking", r"lending", r"trading", r"credit", r"underwriting",
                 r"fraud", r"ledger", r"pci", r"brokerage", r"insurance", r"risk models?"],
     {"fintech": 8}),
    ("aerospace", [r"aerospace", r"avionics", r"do-?178", r"flight", r"satellites?", r"defense", r"uav", r"drones?",
                   r"spacecraft", r"safety.critical", r"rtos", r"embedded", r"firmware", r"fpga", r"c\+\+"],
     {"aerospace": 8, "robotics": 4, "aviation": 4}),
    ("robotics", [r"robotics?", r"ros", r"autonom(y|ous)", r"perception", r"slam", r"motion planning", r"lidar",
                  r"control systems?", r"simulation"],
     {"robotics": 8, "aerospace": 2}),
    ("leadership", [r"p&l", r"roadmaps?", r"hiring", r"org design", r"cross.functional", r"managers? of managers",
                    r"headcount", r"okrs?", r"stakeholders?", r"mentor(ed|ing|ship)", r"direct reports",
                    r"performance reviews?", r"budget", r"team of \d+", r"led a team", r"engineering managers?"],
     {}),
    ("product", [r"product management", r"product managers?", r"prd", r"user research", r"discovery",
                 r"product strategy", r"go.to.market", r"customer interviews", r"a/b test(ing|s)?", r"analytics",
                 r"product roadmap", r"backlog", r"user stories"],
     {"productivity": 2}),
    ("program-delivery", [r"project management", r"project managers?", r"program managers?", r"pmp", r"agile", r"scrum",
                          r"kanban", r"jira", r"sprint planning", r"risk management", r"delivery", r"milestones?",
                          r"gantt", r"safe", r"waterfall", r"change management"],
     {}),
    ("data-analytics", [r"tableau", r"looker", r"power bi", r"dashboards?", r"analytics", r"a/b test(ing|s)?",
                        r"experimentation", r"statistics", r"pandas", r"excel", r"kpis?", r"data analysis"],
     {"data-infra": 3}),
]

# Points-per-hit and cap by rank of the group (0 = strongest in the resume).
DOMAIN_WEIGHTS = [(4, 20), (4, 16), (3, 15), (3, 12), (2, 8), (2, 6)]
MAX_DOMAINS = 6

# Title ladders. Each rung: (name, title regex). Rank order = seniority.
LADDERS = {
    "engineering": [
        ("junior", r"\bjunior\b|\bjr\.?\b|\bassociate\b|\bentry[ -]?level\b|\bintern\b|\bnew grad\b|\bengineer\s*(i|1)\b"),
        ("mid", r"\b(software|backend|back-end|full[ -]?stack|frontend|front-end|platform|data|ml|machine learning|devops|cloud|systems?)\s+(engineer|developer)\b|\bengineer\s*(ii|iii|2|3)\b"),
        ("senior", r"\bsenior\b|\bsr\.?\b"),
        ("staff", r"\b(staff|principal)\b|\btech(nical)? lead\b|\barchitect\b"),
        ("manager", r"\b(engineering|software|development|technical)\s+manager\b|\bmanager,?\s+(software|engineering)\b"),
        ("senior_manager", r"\b(senior|sr\.?)\s+(engineering|software)?\s*manager\b|\bgroup manager\b"),
        ("director", r"\bdirector\b|\bhead of\b"),
        ("vp", r"\bvp\b|\bvice president\b|\bsvp\b"),
        ("executive", r"\bcto\b|\bchief\b"),
    ],
    "product": [
        ("associate_pm", r"\bassociate product manager\b|\bapm\b|\bjunior product\b"),
        ("pm", r"\bproduct (manager|owner)\b"),
        ("senior_pm", r"\b(senior|sr\.?)\s+product (manager|owner)\b"),
        ("principal_pm", r"\b(principal|staff|group|lead)\s+product manager\b"),
        ("director_pm", r"\bdirector(,| of)?\s+product\b|\bhead of product\b"),
        ("vp_product", r"\bvp,?\s+(of\s+)?product\b|\bchief product officer\b|\bcpo\b"),
    ],
    "program": [
        ("coordinator", r"\bproject coordinator\b|\bjunior project\b"),
        ("project_manager", r"\bproject manager\b"),
        ("senior_project_manager", r"\b(senior|sr\.?)\s+project manager\b"),
        ("program_manager", r"\b(technical\s+)?program manager\b|\btpm\b"),
        ("senior_program_manager", r"\b(senior|sr\.?)\s+(technical\s+)?program manager\b"),
        ("director_programs", r"\bdirector(,| of)?\s+(program|project|delivery|pmo)\b|\bhead of (program|delivery)\b"),
    ],
}
# How many points a job title earns by distance from the resume's own rung.
LEVEL_POINTS_BY_DISTANCE = [25, 18, 12, 6]

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
KNOWN_SECTORS = {"healthtech", "security", "data-infra", "ai-native", "devtools", "fintech",
                 "aerospace", "robotics", "vertical-ai", "productivity", "biotech", "medtech",
                 "legaltech", "climate", "retail", "telecom", "aviation", "sales-marketing"}


# --------------------------------------------------------------------------
# Heuristic derivation

def _term_rx(term: str):
    # Fragments are regex already; add boundaries unless they start/end with a
    # symbol (c#, .net, c++) where \b would misfire.
    lead = "" if not term[0].isalnum() else r"\b"
    tail = "" if not term[-1].isalnum() and term[-1] != ")" else r"\b"
    return re.compile(lead + term + tail, re.I)


def _group_hits(text: str):
    """[(group_name, matched_terms, total_hits, score)] sorted strongest first."""
    rows = []
    for name, terms, _ in GROUPS:
        matched, hits = [], 0
        for t in terms:
            n = len(_term_rx(t).findall(text))
            if n:
                matched.append(t)
                hits += n
        if matched:
            score = len(matched) * 3 + min(hits, 25)
            rows.append((name, matched, hits, score))
    rows.sort(key=lambda r: -r[3])
    return rows


def _detect_track(text: str):
    pm = len(re.findall(r"\bproduct (manager|owner|management)\b", text, re.I))
    prog = len(re.findall(r"\b(project|program) (manager|management)\b|\bpmp\b", text, re.I))
    eng = len(re.findall(r"\b(software|engineering|engineer|developer)\b", text, re.I))
    if pm >= 3 and pm > eng / 4:
        return "product"
    if prog >= 3 and prog > eng / 4:
        return "program"
    return "engineering"


def _detect_rung(text: str, track: str) -> int:
    """Index into LADDERS[track] of the rung the resume's titles reach.

    Resumes list the current role first, so a title near the top counts for
    more than one further down: a current "Senior Software Manager" outranks
    two "Technical Lead" roles from years ago. Adjacent/nested matches are
    one title ("Senior" + "Software Manager") credited to the higher rung.
    """
    ladder = LADDERS[track]
    n = max(len(text), 1)
    spans = []   # (rung_index, start, end)
    for i, (_, rx) in enumerate(ladder):
        for m in re.finditer(rx, text, re.I):
            spans.append((i, m.start(), m.end()))
    # Adjacent matches are one title ("Senior" + "Software Engineer"): credit
    # the group to its highest rung only.
    spans.sort(key=lambda t: (t[1], -t[2]))
    groups = []
    for i, s, e in spans:
        if groups and s <= groups[-1][2] + 2:
            g = groups[-1]
            groups[-1] = (max(g[0], i), g[1], max(g[2], e))
        else:
            groups.append((i, s, e))
    scores = [0] * len(ladder)
    for i, s, e in groups:
        pos = s / n
        scores[i] += 3 if pos < 0.25 else 2 if pos < 0.5 else 1
    best, best_score = (1 if len(ladder) > 1 else 0), 0
    for i, score in enumerate(scores):
        if score and (score > best_score or (score == best_score and i > best)):
            best, best_score = i, score
    return best


def _level_table(track: str, rung: int):
    ladder = LADDERS[track]
    rows = []
    for i, (name, rx) in enumerate(ladder):
        d = abs(i - rung)
        pts = LEVEL_POINTS_BY_DISTANCE[min(d, len(LEVEL_POINTS_BY_DISTANCE) - 1)]
        rows.append((pts, i, name, rx))
    # First match wins in compute_fit, so put the most-rewarded rungs first;
    # among equals prefer the more specific (higher) rung.
    rows.sort(key=lambda r: (-r[0], -r[1]))
    return [[rx, pts] for pts, _, _, rx in rows], ladder[rung][0]


def _detect_location(text: str):
    head = text[:1200]
    # "City, ST" at a line start / after a separator first (so "Senior Software
    # Manager Clearwater, FL" doesn't yield a city called "Manager Clearwater"),
    # then any single capitalized word before ", ST".
    for pat in (r"(?:^|\n|[|•·,]\s*)([A-Z][a-zA-Z.]+(?: [A-Z][a-zA-Z.]+)?),\s*([A-Z]{2})\b",
                r"\b([A-Z][a-zA-Z.]+),\s*([A-Z]{2})\b"):
        for m in re.finditer(pat, head):
            if m.group(2) in US_STATES:
                return m.group(1).strip(), m.group(2)
    for st, name in US_STATES.items():
        if re.search(rf"\b{name}\b", head):
            return "", st
    return "", ""


def _location_re(city: str, st: str) -> str:
    parts = []
    if city:
        parts.append(re.escape(city.lower()))
    if st:
        parts.append(re.escape(US_STATES[st].lower()))
        parts.append(rf"\b{st.lower()}\b")
    return "|".join(parts)


def derive_heuristic(text: str, label: str = "") -> tuple[dict, dict]:
    """Returns (spec, summary). `spec` is a JSON-shaped fit profile."""
    track = _detect_track(text)
    rung = _detect_rung(text, track)
    level, rung_name = _level_table(track, rung)
    groups = _group_hits(text)
    strong = [g for g in groups if len(g[1]) >= 2][:MAX_DOMAINS]
    if not strong:
        strong = groups[:3]
    domains = []
    for rank, (name, matched, hits, _) in enumerate(strong):
        per, cap = DOMAIN_WEIGHTS[min(rank, len(DOMAIN_WEIGHTS) - 1)]
        rx = "|".join(_boundary(t) for t in matched)
        domains.append([rx, per, cap])
    sector = {}
    weights_by_group = {g[0]: g[2] for g in GROUPS}
    for rank, (name, *_rest) in enumerate(strong):
        scale = [1.0, 0.8, 0.6, 0.5, 0.4, 0.3][min(rank, 5)]
        for sec, pts in weights_by_group[name].items():
            sector[sec] = sector.get(sec, 0) + pts * scale
    sector = {k: max(1, min(10, round(v))) for k, v in
              sorted(sector.items(), key=lambda kv: -kv[1])[:8]}
    city, st = _detect_location(text)
    remote = bool(re.search(r"\bremote\b", text, re.I))
    spec = {
        "label": label or f"Resume ({datetime.now().strftime('%Y-%m-%d')})",
        "level": level,
        "domains": domains,
        "sector": sector,
        "remote_bonus": 8 if remote else 6,
        "location_re": _location_re(city, st),
        "location_bonus": 8 if (city or st) else 0,
    }
    summary = {
        "method": "heuristic",
        "track": track,
        "level": rung_name,
        "groups": [{"name": n, "terms": m, "hits": h} for n, m, h, _ in strong],
        "location": ", ".join(p for p in (city, st) if p),
        "remote": remote,
    }
    return spec, summary


def _boundary(term: str) -> str:
    lead = "" if not term[0].isalnum() else r"\b"
    tail = "" if (not term[-1].isalnum() and term[-1] != ")") else r"\b"
    return f"{lead}{term}{tail}"


# --------------------------------------------------------------------------
# Claude derivation

CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "<= 60 chars, e.g. 'Senior backend engineer, healthcare data'"},
        "track": {"type": "string", "enum": list(LADDERS)},
        "level": {"type": "string", "description": "rung name from the chosen track's ladder"},
        "domains": {
            "type": "array", "maxItems": MAX_DOMAINS,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "terms": {"type": "array", "items": {"type": "string"}, "maxItems": 18,
                              "description": "lowercase keywords/phrases a job posting would use; no regex"},
                    "weight": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["name", "terms", "weight"], "additionalProperties": False,
            },
        },
        "sectors": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object",
                      "properties": {"sector": {"type": "string", "enum": sorted(KNOWN_SECTORS)},
                                     "points": {"type": "integer", "minimum": 1, "maximum": 10}},
                      "required": ["sector", "points"], "additionalProperties": False},
        },
        "city": {"type": "string"},
        "state": {"type": "string", "description": "two-letter US state code or empty"},
        "prefers_remote": {"type": "boolean"},
    },
    "required": ["headline", "track", "level", "domains", "sectors", "city", "state", "prefers_remote"],
    "additionalProperties": False,
}

CLAUDE_SYSTEM = """You turn a resume into a keyword scoring profile for a job-search dashboard.
Every job posting gets 0-100 points: up to 25 for how close its title's seniority is to the
candidate's, points per keyword hit in each "domain" (capped), a bonus for the company's sector,
and small remote/location bonuses. Your job is to pick the domains and terms so that postings a
recruiter would call a strong match for THIS resume score highest.

Rules:
- Domains: 3-6 groups ordered strongest first (weight 5 = the candidate's core expertise, 1 = minor).
  Terms are literal words/phrases as they appear in postings (e.g. "data pipeline", "fhir", "react"),
  lowercase, no regex syntax. Include only skills the resume actually demonstrates.
- Track: "engineering" for IC/eng-management resumes, "product" for product managers,
  "program" for project/program managers. Level must be a rung from that ladder:
  engineering: junior, mid, senior, staff, manager, senior_manager, director, vp, executive
  product: associate_pm, pm, senior_pm, principal_pm, director_pm, vp_product
  program: coordinator, project_manager, senior_project_manager, program_manager, senior_program_manager, director_programs
  Pick the level the candidate is positioned for NOW (usually their current/most recent title).
- Sectors: the company types this candidate is most credible in."""


def claude_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or (Path.home() / ".config" / "anthropic").exists())


def derive_with_claude(text: str, label: str = "") -> tuple[dict, dict]:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=CLAUDE_SYSTEM,
        messages=[{"role": "user", "content": f"<resume>\n{text}\n</resume>"}],
        output_config={"format": {"type": "json_schema", "schema": CLAUDE_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this resume")
    raw = next(b.text for b in response.content if b.type == "text")
    data = json.loads(raw)
    return _spec_from_claude(data, label), {
        "method": "claude", "model": CLAUDE_MODEL,
        "track": data["track"], "level": data["level"],
        "groups": [{"name": d["name"], "terms": d["terms"], "weight": d["weight"]}
                   for d in data["domains"]],
        "location": ", ".join(p for p in (data.get("city"), data.get("state")) if p),
        "remote": bool(data.get("prefers_remote")),
        "headline": data.get("headline", ""),
    }


def _spec_from_claude(data: dict, label: str) -> dict:
    track = data["track"] if data.get("track") in LADDERS else "engineering"
    names = [n for n, _ in LADDERS[track]]
    rung = names.index(data["level"]) if data.get("level") in names else min(2, len(names) - 1)
    level, _ = _level_table(track, rung)
    weight_map = {5: (4, 20), 4: (4, 16), 3: (3, 12), 2: (2, 8), 1: (1, 6)}
    domains = []
    for d in sorted(data.get("domains") or [], key=lambda d: -int(d.get("weight", 1))):
        terms = [t.strip().lower() for t in d.get("terms") or [] if t and t.strip()]
        if not terms:
            continue
        rx = "|".join(_boundary(re.escape(t).replace(r"\ ", r"[ -]?")) for t in terms)
        per, cap = weight_map.get(int(d.get("weight", 1)), (1, 6))
        domains.append([rx, per, cap])
    if not domains:
        raise RuntimeError("Claude returned no usable domains")
    sector = {s["sector"]: int(s["points"]) for s in data.get("sectors") or []
              if s.get("sector") in KNOWN_SECTORS}
    city = (data.get("city") or "").strip()
    st = (data.get("state") or "").strip().upper()
    if st not in US_STATES:
        st = ""
    return {
        "label": label or (data.get("headline") or "Resume").strip()[:80],
        "level": level,
        "domains": domains[:MAX_DOMAINS],
        "sector": sector,
        "remote_bonus": 8 if data.get("prefers_remote") else 6,
        "location_re": _location_re(city, st),
        "location_bonus": 8 if (city or st) else 0,
    }


# --------------------------------------------------------------------------

def derive(text: str, label: str = "", use_claude: bool | None = None) -> tuple[dict, dict]:
    """Best available derivation. use_claude=None means "if available"."""
    want = claude_available() if use_claude is None else use_claude
    if want:
        try:
            spec, summary = derive_with_claude(text, label)
            return spec, summary
        except Exception as e:  # network, auth, schema — fall back, but say so
            spec, summary = derive_heuristic(text, label)
            summary["warning"] = f"Claude derivation failed ({type(e).__name__}: {e}); used heuristic"
            return spec, summary
    return derive_heuristic(text, label)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="derive a fit profile from a resume file")
    ap.add_argument("resume")
    ap.add_argument("--claude", action="store_true", help="force the Claude path")
    ap.add_argument("--no-claude", action="store_true", help="force the heuristic path")
    args = ap.parse_args()
    p = Path(args.resume)
    txt = extract_text(p.name, p.read_bytes())
    spec, summary = derive(txt, label=p.stem, use_claude=True if args.claude else (False if args.no_claude else None))
    print(json.dumps({"summary": summary, "spec": spec}, indent=2))
