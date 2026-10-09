"""Company+title dedupe for Gmail-derived applied rows.

Why: `state.url` is keyed on gmail msg_id, so the same application confirmed
twice (LinkedIn "your application was sent to X" + the ATS's own "thanks for
applying" minutes later, or a company that re-sends on a repost) inserts as two
separate applied rows. msg_id dedupe can't catch that. Gmail rows carry no job
description, so (company, title) + date proximity is the strongest signal we have.

Matching rules:
  exact   — normalized company AND normalized title both match an existing row
  fuzzy   — normalized company matches and one side's title is unknown, within
            WINDOW_DAYS. This is the LinkedIn/ATS pair case.
Anything else is treated as a genuinely new application (same company, two
different known titles = two real applications).
"""
import re
from datetime import datetime, timedelta

WINDOW_DAYS = 3

UNKNOWN_TITLE_MARKERS = (
    "(role unspecified in snippet)",
    "(unknown)",
    "",
)

# Legal-entity noise: safe to strip anywhere, "Saab" and "Saab, Inc." are one
# company. Kept separate from the descriptive words below because those are
# part of the actual name often enough to matter.
_LEGAL_SUFFIXES = re.compile(
    r"\b(corporation|corp|incorporated|inc|llc|l\.l\.c|ltd|limited|co|company)\b",
    re.I,
)

# Descriptive tails: stripping these merges "M3"/"M3 USA" and "Acme"/"Acme
# Technologies", but also "Integer Holdings"/"Integer Technologies", which are
# unrelated companies. Only stripped when the caller opts in.
_DESCRIPTIVE_SUFFIXES = re.compile(
    r"\b(technologies|technology|labs|holdings|group|international|"
    r"usa|us|na)\b",
    re.I,
)

# Req/posting IDs embedded in titles: (R-252248), R0933795, - 338698, (26101946)
_REQ_ID = re.compile(
    r"[\(\[]?\b(?:r[-_]?\d{4,}|req[-_ ]?\d{3,}|cand[-_]?\d{3,}|\d{6,})\b[\)\]]?",
    re.I,
)


def norm_company(s: str, descriptive: bool = True) -> str:
    """Normalized company key, or "" if nothing usable survives.

    descriptive=False keeps words like Technologies/Group/USA, which the
    LinkedIn crawl needs: it sees far more companies than the Gmail path, and
    there "Integer Holdings" and "Integer Technologies" are different firms.
    Callers must treat "" as un-mergeable rather than as a shared key.
    """
    s = (s or "").lower()
    s = s.replace("&", " and ")        # H&P must not collapse onto HP
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _LEGAL_SUFFIXES.sub(" ", s)
    if descriptive:
        s = _DESCRIPTIVE_SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", "", s)


def norm_title(s: str) -> str:
    s = (s or "").strip()
    if s.lower() in [m.lower() for m in UNKNOWN_TITLE_MARKERS]:
        return ""
    s = s.lower()
    s = _REQ_ID.sub(" ", s)
    s = re.sub(r"\((?:remote|hybrid|onsite|open)\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    # senior/sr and manager/mgr drift between the ATS mail and the LinkedIn mail
    s = re.sub(r"\bsr\b", "senior", s)
    s = re.sub(r"\bmgr\b", "manager", s)
    s = re.sub(r"\beng\b", "engineering", s)
    return re.sub(r"\s+", " ", s).strip()


def is_unknown_title(s: str) -> bool:
    return norm_title(s) == ""


def _parse(d: str):
    try:
        return datetime.strptime(d[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def load_index(con):
    """[(url, norm_company, norm_title, datetime|None, raw_company, raw_title)] for gmail rows."""
    rows = con.execute(
        "SELECT s.url, m.company, m.title, s.updated_at "
        "FROM state s JOIN manual_jobs m ON m.url = s.url "
        "WHERE s.url LIKE 'gmail://msg/%'"
    ).fetchall()
    return [
        (url, norm_company(co), norm_title(ti), _parse(dt), co, ti)
        for url, co, ti, dt in rows
    ]


def find_duplicate(index, company: str, title: str, date: str):
    """Return (kind, existing_entry) or (None, None).

    kind is 'exact' or 'fuzzy'. index entries are as produced by load_index();
    append to it as you accept rows so a batch dedupes against itself too.
    """
    nc, nt, dt = norm_company(company), norm_title(title), _parse(date)
    if not nc:
        return None, None
    for entry in index:
        _, e_nc, e_nt, e_dt, _, _ = entry
        if e_nc != nc:
            continue
        if nt and e_nt and nt == e_nt:
            return "exact", entry
        if (not nt or not e_nt) and dt and e_dt:
            if abs((dt - e_dt).total_seconds()) <= WINDOW_DAYS * 86400:
                return "fuzzy", entry
    return None, None


def repost_key(company: str, title: str):
    """Dedupe key for LinkedIn cards: same role re-posted under a new job id.

    Conservative on company (descriptive=False) because the crawl sees the
    whole market, not just places Adam applied. Falls back to the raw lowercase
    string whenever normalization yields nothing, so blank/junk companies and
    unparseable titles stay distinct instead of collapsing into one bucket.
    """
    nc = norm_company(company, descriptive=False) or (company or "").lower().strip()
    nt = norm_title(title) or re.sub(r"\s+", " ", (title or "").lower()).strip()
    return nc, nt


def index_entry(url, company, title, date):
    return (url, norm_company(company), norm_title(title), _parse(date), company, title)
