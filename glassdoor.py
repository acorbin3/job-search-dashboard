#!/usr/bin/env python3
"""
Glassdoor ratings for the companies that show up in job rows.

Glassdoor has no usable public API and blocks plain HTTP with a bot wall, so
this fetches with the cookie jar lifted out of Chrome by
`tools/gd_session_from_chrome.py` (run that first; re-run when fetches start 403ing).

Everything is cached in state.db's `glassdoor` table, keyed by the company
string as it appears in job rows — an ATS slug ("acadiapharmaceuticals") on
scrape tabs, a display name ("IQVIA") on LinkedIn tabs. Nothing is ever fetched
twice inside the TTL, and misses are cached too so a company Glassdoor doesn't
know about is not re-probed on every page load.

Resolution is deliberately paranoid about identity — Glassdoor's typeahead
happily returns "Cartas na Manga" for "carta". A candidate is only accepted if
its own `shortName`, squashed to alphanumerics, matches the company we asked
for. Unverifiable candidates are recorded as a miss rather than guessed at.

CLI:
    python3 glassdoor.py Databricks "Sword Health"   # ad-hoc lookups
    python3 glassdoor.py --stats                     # cache contents
"""

import argparse
import json
import random
import re
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote


CONFIG_PATH = Path.home() / ".config" / "healthtech-jobs" / "glassdoor_session.json"
BASE = "https://www.glassdoor.com"

# A hit is good for a quarter; ratings move slowly. Misses expire sooner —
# small companies do get added to Glassdoor, and a miss may also be a transient
# block that we don't want to freeze in for months.
TTL_OK = timedelta(days=90)
TTL_MISS = timedelta(days=14)
# When both lookup routes come back with *no candidates at all*, that is more
# often Glassdoor throttling — it answers 200 with an empty list rather than
# 429 — than a company it has never heard of. Retried the same day.
TTL_EMPTY = timedelta(hours=12)

# Politeness floor between requests (seconds, plus jitter). Glassdoor starts
# 429ing after a handful of requests a second or two apart, so this is
# deliberately slow — the fetcher runs in the background off a queue, nothing
# waits on it. `_interval` ratchets up (and never back down) for the life of
# the process each time we do get rate-limited.
MIN_INTERVAL = 8.0
MAX_INTERVAL = 30.0
# Waits after a 429, in seconds. Running out of these gives up on the company.
BACKOFFS = (30, 90, 240)

# Squashed-name similarity below which a candidate is not the company we asked
# for. 0.86 keeps "acadiapharmaceuticals"/"Acadia Pharmaceuticals Inc" and
# rejects "carta"/"cartasnamanga".
MATCH_THRESHOLD = 0.86

_last_request = 0.0
_interval = MIN_INTERVAL


class SessionExpired(Exception):
    """Glassdoor served the bot wall instead of the page — cookies are stale."""


class FetchError(Exception):
    """The request itself failed — network error, or an HTTP status we can't
    do anything useful with (a dead employer id 404s here)."""


class RateLimited(Exception):
    """Glassdoor is throttling us. Transient: back off and come back later."""


# ---------------------------------------------------------------- session

def load_session():
    """The saved cookie jar + UA, or None if the extractor hasn't been run."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not data.get("cookies"):
        return None
    return data


def refresh_session(timeout=90):
    """Re-copy the cookie jar out of Chrome, returning the new session or None.

    Glassdoor rotates its clearance cookies every so often, which strands the
    copy taken earlier; the browser's own jar is already up to date, so a
    re-extract is usually all that's needed. The macOS Keychain prompt only
    appears the first time — after that the ACL is cached and this is silent —
    so if it does block, the timeout gives up rather than hanging the caller.
    """
    script = Path(__file__).resolve().parent / "tools" / "gd_session_from_chrome.py"
    try:
        proc = subprocess.run([sys.executable, str(script)],
                              capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return load_session()


def _curl(url, sess, timeout):
    """One GET through curl. Returns (body, status, final_url).

    Not `requests`, deliberately: the bot wall fingerprints the client below
    the HTTP layer, and python's TLS handshake fails it with exactly the same
    cookies that curl sails through on HTTP/1.1 (curl's own HTTP/2 also gets
    403 — the protocol is part of what's being fingerprinted).

    Cookies go in a 0600 config file rather than argv, where `ps` would show
    the whole session to every process on the machine.
    """
    cookie = "; ".join(f"{k}={v}" for k, v in sess["cookies"].items())

    def q(s):                       # curl config quoting
        return s.replace("\\", "\\\\").replace('"', '\\"')

    with tempfile.TemporaryDirectory() as td:
        cfg, body = Path(td) / "cfg", Path(td) / "body"
        with open(os.open(cfg, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write("\n".join([
                f'user-agent = "{q(sess.get("user_agent", ""))}"',
                f'header = "Cookie: {q(cookie)}"',
                'header = "Accept: text/html,application/xhtml+xml,'
                'application/xml;q=0.9,*/*;q=0.8"',
                'header = "Accept-Language: en-US,en;q=0.9"',
                f'header = "Referer: {BASE}/Reviews/index.htm"',
            ]) + "\n")
        try:
            proc = subprocess.run(
                ["curl", "-sS", "--http1.1", "--compressed", "-L",
                 "--max-time", str(timeout), "-K", str(cfg),
                 "-o", str(body), "-w", "%{http_code} %{url_effective}", url],
                capture_output=True, text=True, timeout=timeout + 10)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise FetchError(f"curl failed: {e}")
        if proc.returncode != 0:
            raise FetchError(f"curl exit {proc.returncode}: "
                             f"{proc.stderr.strip()[:120]}")
        status, _, final_url = proc.stdout.partition(" ")
        text = body.read_text(errors="replace") if body.exists() else ""
    return text, int(status), final_url or url


def _get(path, sess, timeout=25):
    """One throttled, cookie-authenticated GET. Returns (text, final_url).

    Rate limiting is expected, not exceptional: a 429 waits it out and retries,
    and permanently slows the caller down. A 403 is different — that's the bot
    wall, which needs a fresh cookie jar (and a browser that has visited
    Glassdoor recently enough to have one).
    """
    global _last_request, _interval
    for backoff in BACKOFFS + (None,):
        wait = _interval - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))
        _last_request = time.monotonic()
        text, status, final_url = _curl(BASE + path, sess, timeout)
        if status == 429:
            _interval = min(_interval * 1.5, MAX_INTERVAL)
            if backoff is None:
                raise RateLimited("HTTP 429 after every backoff")
            time.sleep(backoff)
            continue
        if status == 403 or "Security | Glassdoor" in text[:2000]:
            raise SessionExpired(f"HTTP {status} — bot wall")
        if status >= 400:
            raise FetchError(f"HTTP {status} for {path}")
        return text, final_url


# ---------------------------------------------------------------- matching

def _squash(name):
    """Lowercase alphanumerics only, with legal-entity suffixes dropped.

    Only true suffixes come off. Words like 'health' or 'labs' are part of the
    identity ('Sword Health' is not 'Sword') and stripping them manufactures
    false matches.
    """
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    s = re.sub(r"\b(inc|llc|lp|ltd|limited|corp|corporation|co|company|plc|"
               r"gmbh|sa|nv|ag|holdings)\b", " ", s)
    return re.sub(r"\s+", "", s)


def _variants(name):
    """Squashed forms to compare on. A leading 'the' survives squashing in a
    slug ('theathletic') but is a separate word in the display name ('The
    Athletic'), so both readings are offered."""
    sq = _squash(name)
    out = {sq}
    if sq.startswith("the") and len(sq) > 5:
        out.add(sq[3:])
    return {v for v in out if v}


def _deslug(name):
    """'sword-health' -> 'sword health'. Separator-split only: concatenated
    slugs like 'acadiapharmaceuticals' can't be split reliably, and Glassdoor's
    typeahead often handles those on its own."""
    return re.sub(r"[-_.]+", " ", name or "").strip()


def name_matches(query, candidate):
    for q in _variants(query):
        for c in _variants(candidate):
            if q == c:
                return True
            # A prefix relationship only counts when almost nothing is left
            # over — 'carta' is a prefix of 'cartasnamanga', and they are
            # different companies.
            if (q.startswith(c) or c.startswith(q)) and abs(len(q) - len(c)) <= 2:
                return True
            if SequenceMatcher(None, q, c).ratio() >= MATCH_THRESHOLD:
                return True
    return False


# ---------------------------------------------------------------- fetching

def search_employer(name, sess):
    """First typeahead company hit for `name`, or None. Used by --check."""
    for hit in _typeahead(name, sess):
        return hit
    return None


def _typeahead(name, sess):
    """Candidate {employer_id, name} dicts from the typeahead endpoint."""
    path = ("/searchsuggest/typeahead?numSuggestions=5&source=GD_V2"
            f"&version=NEW&rf=full&fallback=token&input={quote(name)}")
    try:
        payload = json.loads(_get(path, sess)[0])
    except (json.JSONDecodeError, FetchError):
        return []
    out = []
    for row in payload if isinstance(payload, list) else []:
        if row.get("category") == "company" and row.get("employerId"):
            out.append({"employer_id": int(row["employerId"]),
                        "name": row.get("suggestion") or ""})
    return out


def _search_page(name, sess):
    """Fallback: employer ids scraped off the site search page. The typeahead
    silently returns [] for some real companies ('datavant', 'ro')."""
    try:
        html = _get(f"/Search/results.htm?keyword={quote(name)}",
                    sess)[0]
    except FetchError:
        return []
    seen, out = set(), []
    for m in re.finditer(r"EI_IE(\d+)", html):
        eid = int(m.group(1))
        if eid not in seen:
            seen.add(eid)
            out.append({"employer_id": eid, "name": ""})
        if len(out) >= 4:
            break
    return out


def fetch_employer(employer_id, hint_name, sess):
    """Rating, review count and canonical name for one employer id.

    Uses the id-only Overview URL, which Glassdoor redirects to the canonical
    page — the `Name-Reviews-E<id>` form 404s unless the name slug is exactly
    right, which it never is for an ATS slug. The page carries a precise
    `overallRating`/`reviewCount` in the embedded app state plus a rounded
    EmployerAggregateRating JSON-LD block as fallback.
    """
    html, final_url = _get(f"/Overview/-EI_IE{employer_id}.htm", sess)
    # App state first: it carries the precise rating. The JSON-LD block agrees
    # to within rounding on most companies but sometimes rounds to a whole
    # number (Databricks 4.1 -> "4"), so it's the fallback, not the source.
    rec = (_parse_embedded_rating(html, employer_id)
           or _parse_aggregate_rating(html))
    if not rec:
        return None
    rec.update(employer_id=employer_id, url=final_url)
    return rec


def _dechunk(html):
    """Rejoin the Next.js streamed payload.

    The app state is pushed in slices, so a single JSON record is routinely cut
    in half by markup — `"employerName":"IQV"])</script><script>...push([1,"IA",
    "employerId":8347...`. Splicing the boundaries out puts those records back
    together before anything tries to match them.
    """
    return re.sub(r'"\]\)</script>\s*<script[^>]*>\s*self\.__next_f\.push\(\[1,"',
                  "", html.replace('\\"', '"'))


def _parse_aggregate_rating(html):
    """The page's EmployerAggregateRating JSON-LD: rating, count, and the name
    it belongs to.

    Every number here is attached to a named employer, which matters more than
    it sounds: an overview page also embeds a 'similar companies' rail, each
    entry with its own `overallRating`, so grabbing a loose rating out of the
    HTML can hand back a neighbor's score.
    """
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                         html, re.S):
        try:
            blocks = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        for block in (blocks if isinstance(blocks, list) else [blocks]):
            if (not isinstance(block, dict)
                    or block.get("@type") != "EmployerAggregateRating"):
                continue
            try:
                rating = float(block["ratingValue"])
            except (KeyError, TypeError, ValueError):
                continue
            name = (block.get("itemReviewed") or {}).get("name")
            if not name:
                continue
            count = block.get("ratingCount")
            return {"employer_name": name, "rating": rating,
                    "review_count": int(count) if count else None}
    return None


def _parse_embedded_rating(html, employer_id):
    """The precise rating out of the app state.

    Both patterns tie the number to an identity — the first to this exact
    employer id, the second to the name it sits next to. A bare
    `"overallRating"` match is never used: the 'similar companies' rail on the
    same page carries one per competitor.
    """
    h = _dechunk(html)
    m = re.search(r'"employerName":"([^"]{1,60})","employerId":%d,'
                  r'"overallRating":([0-9.]+),"reviewsCount":([0-9]+)'
                  % employer_id, h)
    if m:
        return {"employer_name": m.group(1), "rating": float(m.group(2)),
                "review_count": int(m.group(3))}
    # The header record: ratings block immediately followed by the name.
    m = re.search(r'"ratings":\{[^{}]*"overallRating":([0-9.]+)\},'
                  r'"shortName":"([^"]{1,60})"', h)
    if not m:
        return None
    count = re.search(r'"reviewsCount":([0-9]+)', h)
    return {"employer_name": m.group(2), "rating": float(m.group(1)),
            "review_count": int(count.group(1)) if count else None}


def resolve(company, sess):
    """Verified Glassdoor record for a company string, or None."""
    return resolve_ex(company, sess)[0]


def resolve_ex(company, sess):
    """(record, saw_candidates) — as `resolve`, plus whether either lookup
    route offered anything at all. No candidates anywhere is a different
    outcome from candidates that failed verification: the first is usually
    throttling, the second is usually a genuine miss.

    Candidates come from the typeahead (raw, then de-slugged) and finally the
    search page; each is confirmed against the company's own `shortName` before
    it's accepted, so a fuzzy near-match never lands in the cache as fact.
    """
    seen = set()
    deslugged = _deslug(company)

    def verify(candidates, limit):
        for cand in candidates[:limit]:
            try:
                rec = fetch_employer(cand["employer_id"],
                                     cand["name"] or deslugged or company, sess)
            except FetchError:
                continue                # dead id — try the next candidate
            if rec and name_matches(company, rec["employer_name"]):
                return rec
        return None

    typeahead = []
    queries = [company]
    if deslugged.lower() != company.lower():
        queries.append(deslugged)
    for q in queries:
        for hit in _typeahead(q, sess):
            if hit["employer_id"] not in seen:
                seen.add(hit["employer_id"])
                typeahead.append(hit)
    # Hits whose names already look right go first; they're near-certain.
    typeahead.sort(key=lambda c: not name_matches(company, c["name"]))
    rec = verify(typeahead, 3)
    if rec:
        return rec, True

    # The typeahead index is both incomplete ('datavant' returns nothing) and
    # stale (it still maps 'Tempus' to an id Glassdoor has since merged into
    # another company). Its failure — not just its silence — is what earns the
    # 400 KB search page.
    fallback = [h for h in _search_page(deslugged or company, sess)
                if h["employer_id"] not in seen]
    return verify(fallback, 3), bool(typeahead or fallback)


# ---------------------------------------------------------------- cache

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glassdoor (
            company       TEXT PRIMARY KEY,
            employer_id   INTEGER,
            employer_name TEXT,
            rating        REAL,
            review_count  INTEGER,
            url           TEXT,
            status        TEXT NOT NULL,     -- ok | notfound | error
            fetched_at    TEXT NOT NULL
        )
    """)
    # The work queue lives here rather than in the server's memory: a full
    # sweep is a multi-day trickle, and a restart in the middle of it should
    # resume, not start over.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glassdoor_queue (
            company    TEXT PRIMARY KEY,
            rank       INTEGER NOT NULL,      -- lower is fetched sooner
            attempts   INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            added_at   TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_glassdoor_queue_rank "
                 "ON glassdoor_queue(rank)")
    conn.commit()


def _key(company):
    return (company or "").strip().lower()


def _fresh(row):
    ttl = {"ok": TTL_OK, "empty": TTL_EMPTY}.get(row["status"], TTL_MISS)
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
    except (TypeError, ValueError):
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched < ttl


def get_cached(conn, companies):
    """{company_key: row-dict} for whatever is already cached and fresh."""
    out = {}
    keys = [_key(c) for c in companies if _key(c)]
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        q = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT * FROM glassdoor WHERE company IN ({q})", chunk):
            row = dict(row)
            if _fresh(row):
                out[row["company"]] = row
    return out


def store(conn, company, rec, status="ok"):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec = rec or {}
    conn.execute("""
        INSERT INTO glassdoor(company, employer_id, employer_name, rating,
                              review_count, url, status, fetched_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            employer_id = excluded.employer_id,
            employer_name = excluded.employer_name,
            rating = excluded.rating,
            review_count = excluded.review_count,
            url = excluded.url,
            status = excluded.status,
            fetched_at = excluded.fetched_at
    """, (_key(company), rec.get("employer_id"), rec.get("employer_name"),
          rec.get("rating"), rec.get("review_count"), rec.get("url"),
          status, now))
    conn.commit()


# ---------------------------------------------------------------- queue
#
# Ordering is by `rank`, lowest first. The backfill hands out ranks in
# newest-posting-first order starting at BACKFILL_RANK; a company on the tab
# being viewed is pushed in front of everything currently waiting.

BACKFILL_RANK = 1_000_000
MAX_ATTEMPTS = 3          # a company that keeps failing gets dropped, not stuck


def queue_add(conn, companies, front=False):
    """Queue companies in the order given. Returns how many were newly added.

    Re-queuing a company already waiting only ever raises its priority, so a
    tab visit can promote work the sweep hasn't reached yet.
    """
    companies = [c for c in companies if (c or "").strip()]
    if not companies:
        return 0
    if front:
        row = conn.execute("SELECT MIN(rank) m FROM glassdoor_queue").fetchone()
        base = (row["m"] if row and row["m"] is not None else BACKFILL_RANK) - len(companies)
    else:
        base = BACKFILL_RANK
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    before = queue_len(conn)
    for i, company in enumerate(companies):
        conn.execute("""
            INSERT INTO glassdoor_queue(company, rank, added_at)
            VALUES(?, ?, ?)
            ON CONFLICT(company) DO UPDATE SET rank = MIN(rank, excluded.rank)
        """, (_key(company), base + i, now))
    conn.commit()
    return queue_len(conn) - before


def queue_next(conn):
    """The company to fetch next, or None when the queue is empty."""
    row = conn.execute("SELECT company FROM glassdoor_queue "
                       "ORDER BY rank LIMIT 1").fetchone()
    return row["company"] if row else None


def queue_done(conn, company):
    conn.execute("DELETE FROM glassdoor_queue WHERE company = ?", (_key(company),))
    conn.commit()


def queue_failed(conn, company, error):
    """Record a failed attempt; drop the company once it's clearly hopeless so
    it can't sit at the head of the queue forever."""
    conn.execute("UPDATE glassdoor_queue SET attempts = attempts + 1, "
                 "last_error = ? WHERE company = ?",
                 (str(error)[:200], _key(company)))
    conn.execute("DELETE FROM glassdoor_queue WHERE company = ? AND attempts >= ?",
                 (_key(company), MAX_ATTEMPTS))
    conn.commit()


def queue_len(conn):
    return conn.execute("SELECT COUNT(*) n FROM glassdoor_queue").fetchone()["n"]


def lookup(conn, company, sess, force=False):
    """Cached record for one company, fetching only on a cache miss.

    Returns the stored row dict (status 'ok' | 'notfound' | 'error').
    """
    key = _key(company)
    if not key:
        return None
    if not force:
        cached = get_cached(conn, [company]).get(key)
        if cached:
            return cached
    try:
        rec, saw_candidates = resolve_ex(company, sess)
    except SessionExpired:
        raise
    except FetchError as e:
        store(conn, company, None, status="error")
        return dict(company=key, status="error", employer_id=None,
                    employer_name=str(e)[:80], rating=None, review_count=None,
                    url=None, fetched_at="")
    status = "ok" if rec else ("notfound" if saw_candidates else "empty")
    store(conn, company, rec, status=status)
    return get_cached(conn, [company]).get(key)


# ---------------------------------------------------------------- cli

def _conn():
    conn = sqlite3.connect(Path(__file__).resolve().parent / "state.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("companies", nargs="*", help="company names or ATS slugs")
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    ap.add_argument("--stats", action="store_true", help="summarize the cache")
    args = ap.parse_args()

    conn = _conn()
    if args.stats:
        rows = conn.execute("SELECT status, COUNT(*) n FROM glassdoor "
                            "GROUP BY status ORDER BY n DESC").fetchall()
        total = sum(r["n"] for r in rows)
        print(f"{total} companies cached")
        for r in rows:
            print(f"  {r['status']:<9} {r['n']}")
        return
    if not args.companies:
        ap.error("give at least one company, or --stats")

    sess = load_session()
    if not sess:
        raise SystemExit("No Glassdoor session. Run: python3 tools/gd_session_from_chrome.py")
    for company in args.companies:
        try:
            row = lookup(conn, company, sess, force=args.force)
        except SessionExpired as e:
            raise SystemExit(f"Session rejected ({e}). Re-run "
                             f"tools/gd_session_from_chrome.py to refresh cookies.")
        except RateLimited as e:
            raise SystemExit(f"Rate limited ({e}). Give it a few minutes.")
        if row and row["status"] == "ok":
            print(f"{company:<28} {row['rating']}  "
                  f"{row['review_count']} ratings  {row['employer_name']}  "
                  f"{row['url']}")
        else:
            print(f"{company:<28} {(row or {}).get('status', 'notfound')}")


if __name__ == "__main__":
    sys.exit(main())
