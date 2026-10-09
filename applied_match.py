"""Flag a crawled job as already-applied by cross-referencing two sources:

  1. state.db `state` rows with status='applied' (company/title via the same
     index the /applied page uses) — full job-search cycle, but the scraped
     company/title can be dirty (domains, page titles).
  2. `applied_signals` — company/role parsed from Gmail confirmation emails.
     Cleaner, but only as far back as the Gmail scan reached.

Returns one of:
  "yes"   — same LinkedIn posting (id match), or same company AND a title that
            clearly matches something applied to there.
  "maybe" — same company, and either a shared meaningful title word or an
            application there with no role captured to compare.
  None    — no evidence.

The "maybe" tier requires that weak title signal by design: without it, one
application to a big employer would flag every role there.
"""
import re
import json

# Words too generic to count as a shared title signal on their own.
_STOP = {"of", "the", "and", "for", "at", "to", "a", "in", "on", "with", "an",
         "senior", "sr", "staff", "principal", "lead", "manager", "director",
         "head", "vp", "vice", "president", "of", "role", "position", "i", "ii",
         "iii", "remote", "us", "engineer"}   # note: "engineering" kept


def norm_company(s):
    """Loose company key: lowercase, drop www/TLD/legal suffixes, keep alnum."""
    s = (s or "").lower().strip()
    s = re.sub(r"^www\.", "", s)
    s = re.sub(r"\.(com|io|net|org|ai|co|health|us)$", "", s)
    s = re.sub(r"[.,]?\s*\b(inc|llc|ltd|corp|co|gmbh|plc|lp|the)\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    # Drop a trailing generic word so "Acme Technologies" ~ "Acme".
    s = re.sub(r"\s+(technologies|technology|software|health|labs|group|"
               r"solutions|systems|sciences|therapeutics|pharma)$", "", s)
    return s.strip()


def _tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if w not in _STOP and len(w) >= 3}


def _looks_like_junk_title(t):
    """State.db titles are often page titles, not roles."""
    t = (t or "").lower()
    return (not t or "|" in t or "job listings" in t or "see job" in t
            or "careers" in t or "want to join" in t or t.endswith(" jobs"))


# For "yes" we must not conflate an IC role with a manager/director role, so
# compare on fuller tokens that KEEP the level/type words (manager, engineer,
# director, ...) — only trivial glue words are dropped.
_GLUE = {"of", "the", "and", "for", "at", "to", "a", "in", "on", "with", "an",
         "ii", "iii"}


def _full_tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if w not in _GLUE and len(w) >= 2}


def _strong(title, applied_role):
    """True if the crawl title clearly denotes the *same* applied role.

    Keeps level/type words so "Software Engineer" (IC) and "Engineering
    Manager" (manager) don't count as the same role just for sharing "data".
    """
    a, b = (title or "").lower().strip(), (applied_role or "").lower().strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ta, tb = _full_tokens(title), _full_tokens(applied_role)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.7


def build_index(conn, all_jobs_index):
    """Return the structure flag() needs.

    all_jobs_index: url -> {company, title, ...}, the same dict server.py builds
    for the /applied page (so scraped company/title match what the app shows).
    """
    # Companies with at least one clean applied role, and a role-less set.
    roles = {}              # norm_company -> set(applied role strings)
    company_only = set()    # norm_company applied to but no clean role
    li_ids = set()          # linkedin posting ids applied to (exact match)

    def add(company, title):
        nc = norm_company(company)
        if not nc:
            return
        if title and not _looks_like_junk_title(title):
            roles.setdefault(nc, set()).add(title)
        else:
            company_only.add(nc)

    # 1. state.db applied rows
    for (url,) in conn.execute("SELECT url FROM state WHERE status='applied'"):
        meta = all_jobs_index.get(url, {})
        add(meta.get("company", ""), meta.get("title", ""))
        m = re.search(r"linkedin\.com/jobs/view/(\d+)", url)
        if m:
            li_ids.add(m.group(1))

    # 2. Gmail confirmation signals
    try:
        for company, role in conn.execute(
                "SELECT company, role FROM applied_signals"):
            add(company, role)
    except Exception:
        pass          # table may not exist yet

    return {"roles": roles, "company_only": company_only, "li_ids": li_ids}


def flag(job, index):
    """job: dict with at least 'company','title','url'. Returns (tier, why)."""
    url = job.get("url", "")
    m = re.search(r"linkedin\.com/jobs/view/(\d+)", url)
    if m and m.group(1) in index["li_ids"]:
        return "yes", "same LinkedIn posting"

    nc = norm_company(job.get("company", ""))
    if not nc:
        return None, ""
    applied_roles = index["roles"].get(nc, set())
    title = job.get("title", "")

    for r in applied_roles:
        if _strong(title, r):
            return "yes", f"applied: {r}"
    for r in applied_roles:
        if _tokens(title) & _tokens(r):
            return "maybe", f"applied here: {r}"
    if nc in index["company_only"] or applied_roles:
        # Applied at this company; either no role to compare, or a different
        # role. Company-level "maybe".
        return "maybe", "applied to this company"
    return None, ""


if __name__ == "__main__":
    # Smoke test against the two LinkedIn crawl files.
    import sqlite3, sys, pathlib
    ROOT = pathlib.Path(__file__).resolve().parent
    conn = sqlite3.connect(ROOT / "state.db")
    conn.row_factory = sqlite3.Row
    # Minimal stand-in for server's _all_jobs_index during CLI testing.
    idx = {}
    for r in conn.execute("SELECT url, company, title FROM linkedin_jobs"):
        idx[r["url"]] = {"company": r["company"], "title": r["title"]}
    index = build_index(conn, idx)
    for prof in ("li_sse_health", "li_adjunct"):
        jobs = json.load(open(ROOT / f".latest_{prof}.json"))
        counts = {"yes": 0, "maybe": 0, None: 0}
        hits = []
        for j in jobs:
            t, why = flag(j, index)
            counts[t] += 1
            if t:
                hits.append((t, j.get("company"), j.get("title", "")[:40], why[:40]))
        print(f"\n{prof}: {len(jobs)} rows -> "
              f"yes={counts['yes']} maybe={counts['maybe']}")
        for t, c, ti, why in hits[:15]:
            print(f"   [{t:5s}] {c[:20]:20s} {ti:40s} <- {why}")
