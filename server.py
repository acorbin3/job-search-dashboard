#!/usr/bin/env python3
"""
Live dashboard server for the job scraper.

Run:
    python3 server.py            # http://127.0.0.1:5050/
    python3 server.py --port 8080

Endpoints:
    GET  /                       -> redirect to /dashboard/director
    GET  /dashboard/<profile>    -> HTML dashboard for that profile
    GET  /api/jobs/<profile>     -> {"jobs": [...], "generated": "..."}
    GET  /api/state              -> {url: {status, notes, updated_at}, ...}
    POST /api/state              -> body {url, status, notes?} (status=null clears)
    POST /api/scrape/<profile>   -> runs scrape.py, returns {ok, rc}
    GET  /api/scrape/<profile>   -> current scrape status for that profile
    GET  /api/scrapes            -> scrape status for every profile
    GET  /applied                -> HTML page of jobs marked applied
    GET  /api/applied            -> {"jobs": [{url, company, title, manual, source,
                                     stage, interviews: [...], ...}]}
    POST /api/app/stage          -> body {url, stage} set pipeline stage (null clears)
    POST /api/interviews         -> body {url, stage?, date?, notes?} log an interview
    DELETE /api/interviews/<id>  -> remove a logged interview
    POST /api/linkedin/crawl      -> run linkedin_scrape.py (body: keywords[],
                                     urls[], remote, posted_days, min_pay,
                                     min_fit, max_per_query, title_profile)
    GET  /api/linkedin/crawl      -> status + tail of the crawl log
    POST /api/glassdoor           -> body {companies: [...]}; cached ratings now,
                                     queues the misses for background fetch
    GET  /api/glassdoor           -> background fetch status
    POST /api/glassdoor/backfill  -> queue every uncached company in every tab,
                                     newest posting first (also runs at boot)
    POST /api/seen                -> record a LinkedIn job seen/dismissed event
    POST /api/seen/lookup         -> batch lookup of seen jobs by id or hash

State lives in state.db (SQLite):
    state(url PRIMARY KEY, status, notes, updated_at)
    manual_jobs(url PRIMARY KEY, company, title, source, added_at)
    ats_cache(company_norm PK, ats, slug, checked_at)
    search_cache((company_norm,title_norm) PK, resolved_url, checked_at)
    linkedin_seen(linkedin_id PK, hash, company, title, location, action,
                  first_seen_at, last_seen_at, seen_count)
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request

import applied_match
import fit_profiles
import glassdoor
import linkedin_scrape
import resume_fit
import scrape

ROOT = Path(__file__).parent
DB_PATH = ROOT / "state.db"

PROFILES_FILE = ROOT / "profiles.json"
TEMPLATES_FILE = ROOT / "data" / "profile_templates.json"
RESUMES_DIR = ROOT / "resumes"


def load_profiles() -> dict:
    """Full tab config keyed by tab key, read fresh so UI edits take effect live.

    {} when nothing is configured yet — the UI then offers starter templates.
    """
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text())
            if isinstance(data, dict):
                return {k: scrape.migrate_profile(v) for k, v in data.items()
                        if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_templates() -> list:
    try:
        data = json.loads(TEMPLATES_FILE.read_text())
        return [t for t in data.get("templates", []) if isinstance(t, dict) and t.get("key")]
    except (json.JSONDecodeError, OSError):
        return []


def first_tab_key(profiles: dict | None = None) -> str | None:
    """Where "/" should land: the first visible tab, else any tab, else None."""
    profiles = profiles if profiles is not None else load_profiles()
    for k, v in profiles.items():
        if not v.get("hidden"):
            return k
    return next(iter(profiles), None)


def resolve_fit(cfg: dict) -> tuple[str, str]:
    """(key, label) of the fit profile that scores this tab."""
    labels = fit_profiles.labels()
    key = cfg.get("fit") or fit_profiles.default_key()
    if key not in labels:
        key = fit_profiles.default_key()
    return key, labels.get(key, key)


def save_profiles(profiles: dict) -> None:
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2))


def profile_tabs(profiles: dict | None = None) -> list:
    """List of {key, label, short} for the tab bar, in file order."""
    profiles = profiles if profiles is not None else load_profiles()
    # Tabs flagged "hidden" stay in profiles.json (and reachable by URL, and
    # still scraped) but drop out of every page's tab bar.
    return [{"key": k,
             "label": v.get("label", k),
             "short": v.get("short", k)} for k, v in profiles.items()
            if not v.get("hidden")]


# Human-readable summary of a tab's filter config — the "prompt" behind Refresh.
def profile_prompt(cfg: dict) -> dict:
    return {
        "level": cfg.get("level", []),
        "domain": cfg.get("domain", []),
        "exclude": cfg.get("exclude", []),
        "min_ai_hits": int(cfg.get("min_ai_hits", 1)),
        "require_etl_signal": bool(cfg.get("require_etl_signal", False)),
        "require_healthtech_sector": bool(cfg.get("require_healthtech_sector", False)),
        "require_remote_or_local": bool(cfg.get("require_remote_or_local", False)),
        "local_re": cfg.get("local_re") or "",
        "health_adjacent_ok": bool(cfg.get("health_adjacent_ok", False)),
        "fit": cfg.get("fit") or "",
        "sources": sorted(scrape.profile_sources(cfg)),
        "linkedin_keywords": list(cfg.get("linkedin_keywords") or []),
    }

ALLOWED_STATUS = {"applied", "ignored", "starred"}

# Pipeline stage of an application (sub-state of status='applied').
# Order matters: the UI renders the dropdown in this order.
APP_STAGES = [
    "applied",
    "recruiter_screen",
    "hiring_manager",
    "technical",
    "panel",
    "offer",
    "rejected",
    "withdrawn",
    "ghosted",
]

# In-memory tracking of scrape jobs. One slot per profile.
#   {profile: {"status": "running"|"done"|"error", "pid": int,
#              "started_at": iso, "finished_at": iso|None, "rc": int|None}}
SCRAPES = {}
SCRAPES_LOCK = threading.Lock()


app = Flask(__name__)


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _progress_path(profile):
    return ROOT / f".scrape_progress_{profile}.json"


def _read_progress(profile):
    try:
        return json.loads(_progress_path(profile).read_text())
    except (OSError, ValueError):
        return None


def _run_scrape(profile, out_path):
    progress_path = _progress_path(profile)
    progress_path.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scrape.py"),
         "--profile", profile, "--json", str(out_path),
         "--progress-file", str(progress_path)],
        cwd=str(ROOT),
    )
    with SCRAPES_LOCK:
        SCRAPES[profile]["pid"] = proc.pid
    rc = proc.wait()
    with SCRAPES_LOCK:
        SCRAPES[profile].update(
            status="done" if rc == 0 else "error",
            finished_at=_iso_now(),
            rc=rc,
        )


def _latest_generated(profile):
    path = ROOT / f".latest_{profile}.json"
    if not path.exists():
        return None
    return datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                url TEXT PRIMARY KEY,
                status TEXT,
                notes TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_jobs (
                url TEXT PRIMARY KEY,
                company TEXT,
                title TEXT,
                source TEXT,
                added_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS linkedin_seen (
                linkedin_id TEXT PRIMARY KEY,
                hash TEXT,
                company TEXT,
                title TEXT,
                location TEXT,
                action TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                seen_count INTEGER DEFAULT 1
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_linkedin_seen_hash "
            "ON linkedin_seen(hash)"
        )
        # Captured application-form answers (Learn mode). One row per
        # normalized question; `variants` keeps every distinct value we've
        # seen with its count so the review UI can surface the usual answer.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS answers (
                question_key TEXT PRIMARY KEY,
                label TEXT,
                field_type TEXT,
                value TEXT,
                variants TEXT,
                domains TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                seen_count INTEGER DEFAULT 1
            )
        """)
        # Pipeline stage lives on the state row; one interviews row per round.
        state_cols = {r[1] for r in conn.execute("PRAGMA table_info(state)")}
        if "stage" not in state_cols:
            conn.execute("ALTER TABLE state ADD COLUMN stage TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS interviews (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                stage TEXT,
                happened_at TEXT,
                notes TEXT,
                created_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interviews_url ON interviews(url)"
        )
        # Small key/value store for UI config that must survive a reload
        # (e.g. the LinkedIn crawl's keyword list).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        # One row per LinkedIn crawl, so "when did it last run and for how
        # long" survives a server restart (SCRAPES is in-memory only).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS linkedin_runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_sec INTEGER,
                status TEXT NOT NULL,
                rc INTEGER,
                params TEXT,
                keywords TEXT,
                cards INTEGER,
                published INTEGER,
                queries_total INTEGER,
                profile TEXT,
                pid INTEGER
            )
        """)
        # Databases created before per-tab crawls lack these two columns.
        have = {r[1] for r in conn.execute("PRAGMA table_info(linkedin_runs)")}
        for col, typ in (("profile", "TEXT"), ("pid", "INTEGER")):
            if col not in have:
                conn.execute(f"ALTER TABLE linkedin_runs ADD COLUMN {col} {typ}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_linkedin_runs_started "
            "ON linkedin_runs(started_at DESC)"
        )
        # A crawl marked running at boot belongs to a dead server — its
        # subprocess died with it, so it will never report a result.
        conn.execute(
            "UPDATE linkedin_runs SET status = 'interrupted' "
            "WHERE status = 'running'"
        )
        glassdoor.init_db(conn)
        conn.commit()


def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?",
                           (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(value), _iso_now()))
        conn.commit()


@app.route("/")
def home():
    key = first_tab_key()
    return redirect(f"/dashboard/{key}" if key else "/tabs?welcome=1")


@app.route("/dashboard/<profile>")
def dashboard(profile):
    profiles = load_profiles()
    if profile not in profiles:
        if not profiles:
            return redirect("/tabs?welcome=1")
        abort(404)
    fit_key, fit_label = resolve_fit(profiles[profile])
    return render_template(
        "dashboard.html",
        profile=profile,
        profile_label=profiles[profile].get("label", profile),
        fit_key=fit_key,
        fit_label=fit_label,
        fit_is_builtin=(fit_key == fit_profiles.BUILTIN_KEY),
        # "ats" tabs are derived from the shared scan; "linkedin" tabs are fed
        # by linkedin_scrape.py and get the crawl panel instead of the ATS
        # refresh buttons. Gate on this, never on the tab's name.
        source=scrape.profile_source(profiles[profile]),
        ats_enabled="ats" in scrape.profile_sources(profiles[profile]),
        li_enabled="linkedin" in scrape.profile_sources(profiles[profile]),
        profiles=profile_tabs(profiles),
        prompt=profile_prompt(profiles[profile]),
        # Most tabs want the Remote-only default. Tabs whose sources don't set
        # LinkedIn's remote flag reliably — faculty postings barely ever do —
        # can opt out with "remote_default" so the page isn't empty on arrival.
        remote_default=profiles[profile].get("remote_default", "remote"),
        # Healthcare term patterns, so the expanded description can highlight
        # the actual matched words (health_hits holds labels, not text).
        health_terms=[rx.pattern for rx, _ in scrape.HEALTH_TERMS],
    )


@app.route("/tabs")
def tabs_page():
    profiles = load_profiles()
    return render_template("tabs.html", profiles=profile_tabs(profiles),
                           has_profiles=bool(profiles),
                           welcome=bool(request.args.get("welcome")) or not profiles)


_LIST_FIELDS = ("level", "domain", "exclude")
_FLAG_FIELDS = ("require_etl_signal", "require_healthtech_sector",
                "require_remote_or_local", "health_adjacent_ok")
_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def _clean_profile_payload(body: dict, existing: dict | None = None):
    """Validate + normalize a tab config from the UI. Returns (cfg, error).

    `existing` is the current stored config, if any. This function rebuilds the
    entry from a fixed field list, so anything it does not know about is
    dropped — which used to silently strip `source`/`external` off a LinkedIn
    tab and hand it back to the ATS scan to overwrite. Fields the UI does not
    edit are carried over from `existing` instead.
    """
    label = (body.get("label") or "").strip()
    short = (body.get("short") or "").strip()
    if not label:
        return None, "Label is required"
    cfg = {"label": label, "short": short or label[:12]}
    # Sources: new payloads send the booleans; older ones (and legacy files)
    # send/hold `source`. Normalize to the booleans and keep `source` in step
    # so scrape.profile_source() agrees.
    if "ats" in body or "linkedin" in body:
        ats_on, li_on = bool(body.get("ats", True)), bool(body.get("linkedin", False))
    elif body.get("source") in ("ats", "linkedin"):
        ats_on, li_on = body["source"] == "ats", body["source"] == "linkedin"
    else:
        srcs = scrape.profile_sources(existing or {})
        ats_on, li_on = "ats" in srcs, "linkedin" in srcs
    if not (ats_on or li_on):
        return None, "pick at least one source (job boards or LinkedIn)"
    cfg["ats"], cfg["linkedin"] = ats_on, li_on
    cfg["source"] = "ats" if ats_on else "linkedin"
    raw_kw = body.get("linkedin_keywords", (existing or {}).get("linkedin_keywords", []))
    if isinstance(raw_kw, str):
        raw_kw = raw_kw.splitlines()
    cfg["linkedin_keywords"] = [k.strip() for k in (raw_kw or []) if k and k.strip()][:20]
    if li_on and not cfg["linkedin_keywords"]:
        cfg["linkedin_keywords"] = [label]
    for f in _LIST_FIELDS:
        raw = body.get(f, [])
        if isinstance(raw, str):
            raw = raw.splitlines()
        patterns = [p.strip() for p in raw if p and p.strip()]
        for p in patterns:
            try:
                re.compile(p)
            except re.error as e:
                return None, f"Invalid regex in {f}: {p!r} ({e})"
        cfg[f] = patterns
    if not cfg["level"] or not cfg["domain"]:
        return None, "Both 'level' and 'domain' need at least one pattern"
    for f in _FLAG_FIELDS:
        cfg[f] = bool(body.get(f, False))
    local_re = (body.get("local_re") or "").strip()
    if local_re:
        try:
            re.compile(local_re)
        except re.error as e:
            return None, f"Invalid regex in local area: {local_re!r} ({e})"
        cfg["local_re"] = local_re
    elif "local_re" in body:
        cfg["local_re"] = ""
    cfg["hidden"] = bool(body.get("hidden", False))
    fit = (body.get("fit") or "").strip()
    if fit and fit not in fit_profiles.labels():
        return None, f"unknown fit profile {fit!r}"
    if fit:
        cfg["fit"] = fit
    elif "fit" in body:      # explicitly cleared -> back to the run default
        cfg["fit"] = ""
    try:
        cfg["min_ai_hits"] = max(0, int(body.get("min_ai_hits", 1)))
    except (TypeError, ValueError):
        cfg["min_ai_hits"] = 1
    # Carry over anything the editor doesn't know about (remote_default,
    # include/seed_from, require_desc/exclude_desc, sector flags, ...) so a
    # save from the Manage Tabs page can't strip a tab's other config.
    for k, v in (existing or {}).items():
        cfg.setdefault(k, v)
    return cfg, None


@app.route("/api/profiles", methods=["GET"])
def api_profiles_get():
    return jsonify({"profiles": load_profiles(),
                    "fit_profiles": fit_profiles.labels(),
                    "default_fit": fit_profiles.default_key(),
                    "builtin_fit": fit_profiles.BUILTIN_KEY,
                    "templates": load_templates()})


@app.route("/api/templates", methods=["GET"])
def api_templates():
    return jsonify({"templates": load_templates()})


@app.route("/api/profiles/from_template", methods=["POST"])
def api_profiles_from_template():
    """Create a tab from profile_templates.json. Body: {template, key?, fit?}."""
    body = request.get_json(force=True, silent=True) or {}
    tpl = next((t for t in load_templates() if t["key"] == body.get("template")), None)
    if not tpl:
        return jsonify({"error": "unknown template"}), 404
    key = (body.get("key") or tpl["key"]).strip().lower()
    if not _KEY_RE.match(key):
        return jsonify({"error": "key must be lowercase letters, digits, or underscores"}), 400
    profiles = load_profiles()
    if key in profiles:
        return jsonify({"error": f"a tab named {key!r} already exists"}), 409
    payload = {k: v for k, v in tpl.items() if k not in ("key", "description")}
    if body.get("fit"):
        payload["fit"] = body["fit"]
    cfg, err = _clean_profile_payload(payload)
    if err:
        return jsonify({"error": err}), 400
    profiles[key] = cfg
    save_profiles(profiles)
    return jsonify({"ok": True, "key": key, "profile": cfg})


@app.route("/api/profiles/<key>", methods=["POST"])
def api_profiles_post(key):
    if not _KEY_RE.match(key):
        return jsonify({"error": "key must be lowercase letters, digits, or underscores"}), 400
    body = request.get_json(force=True, silent=True) or {}
    profiles = load_profiles()
    cfg, err = _clean_profile_payload(body, profiles.get(key))
    if err:
        return jsonify({"error": err}), 400
    profiles[key] = cfg
    save_profiles(profiles)
    return jsonify({"ok": True, "key": key, "profile": cfg})


@app.route("/api/profiles/<key>", methods=["DELETE"])
def api_profiles_delete(key):
    profiles = load_profiles()
    if key not in profiles:
        return jsonify({"error": "not found"}), 404
    # Deleting the last tab is fine: "/" then lands on the starter templates.
    del profiles[key]
    save_profiles(profiles)
    return jsonify({"ok": True})


@app.route("/api/jobs/<profile>")
def api_jobs(profile):
    if profile not in load_profiles():
        abort(404)
    cfg = load_profiles()[profile]
    srcs = scrape.profile_sources(cfg)
    # A tab's rows come from up to two files: the ATS scan's .latest_<key>.json
    # and the LinkedIn crawl's sibling. A LinkedIn-only tab (legacy) writes the
    # plain file itself. Merge by URL; newest file sets "generated".
    paths = []
    if "ats" in srcs:
        paths.append(ROOT / f".latest_{profile}.json")
        if "linkedin" in srcs:
            paths.append(li_out_path(profile))
    else:
        paths.append(ROOT / f".latest_{profile}.json")
    paths = [p for p in paths if p.exists()]
    if not paths:
        return jsonify({"jobs": [], "generated": "never"})
    jobs, seen = [], set()
    for p in paths:
        try:
            rows = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for j in rows:
            key = j.get("url") or f"{j.get('company')}|{j.get('title')}"
            if key in seen:
                continue
            seen.add(key)
            jobs.append(j)
    mtime = datetime.fromtimestamp(
        max(p.stat().st_mtime for p in paths), tz=timezone.utc
    ).isoformat(timespec="seconds")
    # LinkedIn rows get an already-applied flag, cross-referencing the applied
    # rows + confirmation signals. Cheap: one index build per request, then a
    # dict/regex check per row (no network).
    if "linkedin" in srcs:
        try:
            with db() as conn:
                index = applied_match.build_index(conn, _all_jobs_index())
            for j in jobs:
                if j.get("ats") != "linkedin":
                    continue
                tier, why = applied_match.flag(j, index)
                j["applied_match"] = tier          # "yes" | "maybe" | None
                j["applied_why"] = why
        except Exception as e:            # never let matching break the tab
            app.logger.warning("applied_match failed: %s", e)
    return jsonify({"jobs": jobs, "generated": mtime})


@app.route("/api/state", methods=["GET"])
def api_state_get():
    with db() as conn:
        rows = conn.execute(
            "SELECT url, status, notes, updated_at FROM state"
        ).fetchall()
    return jsonify({r["url"]: dict(r) for r in rows})


@app.route("/api/state", methods=["POST"])
def api_state_post():
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url")
    if not url or not isinstance(url, str):
        return jsonify({"error": "url required"}), 400
    status = body.get("status")
    if status is not None and status not in ALLOWED_STATUS:
        return jsonify({"error": f"status must be null or one of {sorted(ALLOWED_STATUS)}"}), 400
    notes = body.get("notes") or ""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as conn:
        row = conn.execute("SELECT stage FROM state WHERE url = ?", (url,)).fetchone()
        has_stage = bool(row and row["stage"])
        if status is None and not notes and not has_stage:
            conn.execute("DELETE FROM state WHERE url = ?", (url,))
        elif status is None and not notes and has_stage:
            conn.execute(
                "UPDATE state SET status = NULL, updated_at = ? WHERE url = ?",
                (now, url))
        else:
            conn.execute("""
                INSERT INTO state(url, status, notes, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    status = excluded.status,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
            """, (url, status, notes, now))
        conn.commit()
    return jsonify({"ok": True, "url": url, "status": status})


@app.route("/api/scrape/<profile>", methods=["POST"])
def api_scrape(profile):
    if profile not in load_profiles():
        abort(404)
    with SCRAPES_LOCK:
        existing = SCRAPES.get(profile)
        if existing and existing["status"] == "running":
            return jsonify({"started": False, **existing,
                            "generated": _latest_generated(profile)})
        SCRAPES[profile] = {
            "status": "running",
            "pid": None,
            "started_at": _iso_now(),
            "finished_at": None,
            "rc": None,
        }
        snapshot = dict(SCRAPES[profile])
    out_path = ROOT / f".latest_{profile}.json"
    threading.Thread(
        target=_run_scrape, args=(profile, out_path), daemon=True
    ).start()
    return jsonify({"started": True, **snapshot,
                    "generated": _latest_generated(profile)})


# ---------------------------------------------------------------------------
# Fit profiles (resume -> scoring table). See fit_profiles.py / resume_fit.py.

RESUME_MAX_BYTES = 8 * 1024 * 1024


@app.route("/api/fit_profiles", methods=["GET"])
def api_fit_profiles_get():
    return jsonify({"profiles": fit_profiles.load_specs(),
                    "default": fit_profiles.default_key(),
                    "builtin": {fit_profiles.BUILTIN_KEY: fit_profiles.BUILTIN[fit_profiles.BUILTIN_KEY]},
                    "claude_available": resume_fit.claude_available(),
                    "claude_model": resume_fit.CLAUDE_MODEL,
                    "raw_scan": (ROOT / ".raw_scan.json").exists()})


@app.route("/api/fit_profiles/derive", methods=["POST"])
def api_fit_profiles_derive():
    """multipart: resume=<file>, label=<str>, method=auto|heuristic|claude.

    Returns the derived spec + a summary for the user to review; nothing is
    saved until POST /api/fit_profiles/<key>. The upload itself is kept under
    resumes/ (gitignored) so the autofill tooling can reuse it.
    """
    f = request.files.get("resume")
    if not f or not f.filename:
        return jsonify({"error": "no file uploaded"}), 400
    data = f.read(RESUME_MAX_BYTES + 1)
    if len(data) > RESUME_MAX_BYTES:
        return jsonify({"error": "file too large (8 MB max)"}), 413
    try:
        text = resume_fit.extract_text(f.filename, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # corrupt zip, pdf parser blowup, ...
        return jsonify({"error": f"could not read file ({type(e).__name__}: {e})"}), 400
    method = (request.form.get("method") or "auto").lower()
    use_claude = {"auto": None, "heuristic": False, "claude": True}.get(method, None)
    if use_claude and not resume_fit.claude_available():
        return jsonify({"error": "Claude isn't configured: pip install anthropic and set "
                                 "ANTHROPIC_API_KEY (or `ant auth login`)"}), 400
    label = (request.form.get("label") or "").strip() or Path(f.filename).stem
    spec, summary = resume_fit.derive(text, label=label, use_claude=use_claude)
    stored = ""
    try:
        RESUMES_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(f.filename).name) or "resume"
        (RESUMES_DIR / safe).write_bytes(data)
        stored = f"resumes/{safe}"
    except OSError:
        pass
    spec["source"] = stored or f.filename
    return jsonify({"spec": spec, "summary": summary, "chars": len(text),
                    "suggested_key": re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")[:32] or "resume"})


@app.route("/api/fit_profiles/default", methods=["POST"])
def api_fit_profiles_default():
    body = request.get_json(force=True, silent=True) or {}
    try:
        fit_profiles.set_default(body.get("key") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "default": fit_profiles.default_key()})


@app.route("/api/fit_profiles/<key>", methods=["POST"])
def api_fit_profiles_post(key):
    """Body: {spec, make_default?, rescore?}. rescore re-derives every ATS tab
    from the cached raw scan so the Fit column reflects the new table."""
    body = request.get_json(force=True, silent=True) or {}
    spec = body.get("spec") or {}
    try:
        saved = fit_profiles.save(key, spec, make_default=bool(body.get("make_default")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    started = False
    if body.get("rescore") and (ROOT / ".raw_scan.json").exists() and load_profiles():
        started = _start_scrape_all(from_raw=True, fit=None)
    return jsonify({"ok": True, "key": key, "profile": saved,
                    "default": fit_profiles.default_key(), "rescore_started": started})


@app.route("/api/fit_profiles/<key>", methods=["DELETE"])
def api_fit_profiles_delete(key):
    if key == fit_profiles.BUILTIN_KEY:
        return jsonify({"error": "the built-in profile can't be deleted"}), 400
    if not fit_profiles.delete(key):
        return jsonify({"error": "not found"}), 404
    # Tabs that pinned this profile fall back to the run default.
    profiles = load_profiles()
    changed = False
    for cfg in profiles.values():
        if cfg.get("fit") == key:
            cfg["fit"] = ""
            changed = True
    if changed:
        save_profiles(profiles)
    return jsonify({"ok": True, "default": fit_profiles.default_key()})


# One superset scan updates every profile's table (scrape.py --all). Tracked
# under a reserved SCRAPES key so it can't collide with a profile name.
ALL_KEY = "_all"


def _run_scrape_all(from_raw=False, fit=None):
    progress_path = _progress_path("all")
    progress_path.unlink(missing_ok=True)
    cmd = [sys.executable, str(ROOT / "scrape.py"),
           "--from-raw" if from_raw else "--all",
           "--progress-file", str(progress_path)]
    if fit:
        cmd += ["--fit", fit]
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    with SCRAPES_LOCK:
        SCRAPES[ALL_KEY]["pid"] = proc.pid
    rc = proc.wait()
    with SCRAPES_LOCK:
        SCRAPES[ALL_KEY].update(
            status="done" if rc == 0 else "error",
            finished_at=_iso_now(),
            rc=rc,
        )


@app.route("/api/scrape_all", methods=["POST"])
def api_scrape_all():
    body = request.get_json(silent=True) or {}
    from_raw = bool(body.get("from_raw"))
    fit = body.get("fit")
    known = fit_profiles.labels()
    if fit and fit not in known:
        return jsonify({"started": False,
                        "error": f"unknown fit profile {fit!r} — "
                                 f"choices: {sorted(known)}"}), 400
    if not load_profiles():
        return jsonify({"started": False,
                        "error": "no tabs configured yet — create one at /tabs first"}), 409
    if from_raw and not (ROOT / ".raw_scan.json").exists():
        return jsonify({"started": False,
                        "error": "no cached raw scan (.raw_scan.json) — "
                                 "run a full refresh first"}), 409
    snapshot = _start_scrape_all(from_raw, fit)
    if not snapshot:
        with SCRAPES_LOCK:
            return jsonify({"started": False, **SCRAPES[ALL_KEY]})
    return jsonify({"started": True, **snapshot})


def _start_scrape_all(from_raw=False, fit=None):
    """Kick off scrape.py --all/--from-raw in a thread. Returns the status
    snapshot, or None if one is already running."""
    with SCRAPES_LOCK:
        existing = SCRAPES.get(ALL_KEY)
        if existing and existing["status"] == "running":
            return None
        SCRAPES[ALL_KEY] = {
            "status": "running",
            "pid": None,
            "started_at": _iso_now(),
            "finished_at": None,
            "rc": None,
            "mode": "from_raw" if from_raw else "scan",
            "fit": fit or fit_profiles.default_key(),
        }
        snapshot = dict(SCRAPES[ALL_KEY])
    threading.Thread(target=_run_scrape_all, args=(from_raw, fit),
                     daemon=True).start()
    return snapshot


@app.route("/api/scrape_all", methods=["GET"])
def api_scrape_all_status():
    with SCRAPES_LOCK:
        entry = dict(SCRAPES.get(ALL_KEY) or {"status": "idle"})
    if entry["status"] == "running":
        entry["progress"] = _read_progress("all")
    return jsonify(entry)


@app.route("/api/scrape/<profile>", methods=["GET"])
def api_scrape_status(profile):
    if profile not in load_profiles():
        abort(404)
    with SCRAPES_LOCK:
        entry = dict(SCRAPES.get(profile) or {"status": "idle"})
    entry["generated"] = _latest_generated(profile)
    if entry["status"] == "running":
        entry["progress"] = _read_progress(profile)
    return jsonify(entry)


def _all_jobs_index():
    """Build {url: {company, title, ats, manual, source}} from every
    .latest_*.json plus the manual_jobs table.

    .latest_*.json wins on conflict — scraped metadata is usually richer.
    """
    idx = {}
    for path in ROOT.glob(".latest_*.json"):
        try:
            for j in json.loads(path.read_text()):
                u = j.get("url")
                if u and u not in idx:
                    idx[u] = {"company": j.get("company"), "title": j.get("title"),
                              "ats": j.get("ats"), "manual": False, "source": None}
        except Exception:
            continue
    with db() as conn:
        for r in conn.execute(
            "SELECT url, company, title, source FROM manual_jobs"
        ).fetchall():
            if r["url"] not in idx:
                idx[r["url"]] = {
                    "company": r["company"], "title": r["title"],
                    "ats": None, "manual": True, "source": r["source"],
                }
    return idx


@app.route("/applied")
def applied_page():
    return render_template("applied.html", profiles=profile_tabs())


@app.route("/api/applied")
def api_applied():
    with db() as conn:
        rows = conn.execute(
            "SELECT url, notes, stage, updated_at FROM state WHERE status='applied' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        ivs = conn.execute(
            "SELECT id, url, stage, happened_at, notes FROM interviews "
            "ORDER BY happened_at, id"
        ).fetchall()
    by_url = {}
    for iv in ivs:
        by_url.setdefault(iv["url"], []).append({
            "id": iv["id"], "stage": iv["stage"],
            "happened_at": iv["happened_at"], "notes": iv["notes"] or "",
        })
    idx = _all_jobs_index()
    jobs = []
    for r in rows:
        meta = idx.get(r["url"], {})
        interviews = by_url.get(r["url"], [])
        jobs.append({
            "url": r["url"],
            "updated_at": r["updated_at"],
            "notes": r["notes"] or "",
            "stage": r["stage"] or "applied",
            "interviews": interviews,
            "interview_count": len(interviews),
            "company": meta.get("company"),
            "title": meta.get("title"),
            "ats": meta.get("ats"),
            "manual": bool(meta.get("manual")),
            "source": meta.get("source"),
        })
    return jsonify({"jobs": jobs, "stages": APP_STAGES})


@app.route("/api/app/stage", methods=["POST"])
def api_app_stage():
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url")
    if not url or not isinstance(url, str):
        return jsonify({"error": "url required"}), 400
    stage = body.get("stage")
    if stage is not None and stage not in APP_STAGES:
        return jsonify({"error": f"stage must be null or one of {APP_STAGES}"}), 400
    now = _iso_now()
    with db() as conn:
        cur = conn.execute(
            "UPDATE state SET stage = ?, updated_at = updated_at WHERE url = ?",
            (stage, url))
        if cur.rowcount == 0:
            return jsonify({"error": "url not tracked — mark it applied first"}), 404
        conn.commit()
    return jsonify({"ok": True, "url": url, "stage": stage})


@app.route("/api/interviews", methods=["POST"])
def api_interviews_post():
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url")
    if not url or not isinstance(url, str):
        return jsonify({"error": "url required"}), 400
    stage = body.get("stage")
    if stage is not None and stage not in APP_STAGES:
        return jsonify({"error": f"stage must be null or one of {APP_STAGES}"}), 400
    happened_at = (body.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    notes = (body.get("notes") or "").strip()
    now = _iso_now()
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO interviews(url, stage, happened_at, notes, created_at)
            VALUES(?, ?, ?, ?, ?)
        """, (url, stage, happened_at, notes, now))
        # Logging an interview at a stage advances the application to that
        # stage, but never moves it backwards or out of a closed state.
        if stage and stage not in ("rejected", "withdrawn", "ghosted"):
            row = conn.execute(
                "SELECT stage FROM state WHERE url = ?", (url,)).fetchone()
            if row is not None:
                cur_stage = row["stage"] or "applied"
                if (cur_stage in APP_STAGES
                        and cur_stage not in ("offer", "rejected", "withdrawn", "ghosted")
                        and APP_STAGES.index(stage) > APP_STAGES.index(cur_stage)):
                    conn.execute("UPDATE state SET stage = ? WHERE url = ?",
                                 (stage, url))
        conn.commit()
        new_id = cur.lastrowid
    return jsonify({"ok": True, "id": new_id, "url": url,
                    "stage": stage, "happened_at": happened_at})


@app.route("/api/interviews/<int:iv_id>", methods=["DELETE"])
def api_interviews_delete(iv_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM interviews WHERE id = ?", (iv_id,))
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "id": iv_id})


@app.route("/api/scrapes", methods=["GET"])
def api_scrapes_all():
    out = {}
    with SCRAPES_LOCK:
        snapshot = {k: dict(v) for k, v in SCRAPES.items()}
    for p in load_profiles():
        entry = snapshot.get(p) or {"status": "idle"}
        entry["generated"] = _latest_generated(p)
        out[p] = entry
    return jsonify(out)


# --- LinkedIn crawl (linkedin_scrape.py) ---------------------------------
# Its own SCRAPES slot: it's a separate crawler, not an ATS scan, and tabs it
# feeds are source="linkedin" so scrape.py --all leaves them alone.
#
# Everything here is keyed by tab. The manager, healthcare-SWE and adjunct
# searches are independent crawls with their own keywords, cadence and
# history, so sharing one log / progress file / saved form (as this did when
# there was only one LinkedIn tab) made them overwrite each other.


def _li_key(profile):
    return f"_linkedin:{profile}"


def _li_log(profile):
    return ROOT / f".linkedin_crawl_{profile}.log"


def _li_progress_path(profile):
    return ROOT / f".linkedin_crawl_progress_{profile}.json"


def _li_setting_key(profile):
    return f"linkedin_crawl:{profile}"


def li_out_path(profile):
    """Where the LinkedIn crawl writes for a tab. Mixed-source tabs get a
    sibling file so the ATS scan's .latest_<key>.json isn't clobbered."""
    srcs = scrape.profile_sources(load_profiles().get(profile) or {})
    if "ats" in srcs:
        return ROOT / f".latest_{profile}.linkedin.json"
    return ROOT / f".latest_{profile}.json"


def li_session_status():
    """Is a signed-in LinkedIn session on disk? (Guest crawling works without.)"""
    sess = None
    try:
        sess = linkedin_scrape.load_session()
    except Exception:
        sess = None
    return {"signed_in": bool(sess),
            "path": str(linkedin_scrape.SESSION_PATH),
            "how": "python3 tools/li_session_from_chrome.py"}


@app.route("/api/linkedin/session", methods=["GET"])
def api_linkedin_session():
    return jsonify(li_session_status())


def _li_saved(profile):
    """The remembered crawl form for a tab.

    Falls back to the pre-per-tab settings key so the original LinkedIn tab
    keeps the setup it was last run with instead of resetting to defaults.
    """
    saved = get_setting(_li_setting_key(profile))
    if saved is None and profile == "linkedin":
        saved = get_setting("linkedin_crawl")
    return saved or {}


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _li_running():
    """The crawl currently in flight, or None.

    Checked against the DB rather than the in-memory SCRAPES dict: a crawler
    is a child process that outlives a server restart, but SCRAPES does not.
    Restarting the server therefore used to forget about a live crawl and let
    a second one start on the same tab, and the two raced to write the same
    .latest_*.json. The recorded pid is the source of truth.
    """
    with db() as conn:
        for r in conn.execute(
                "SELECT id, profile, started_at, pid FROM linkedin_runs "
                "WHERE status='running' ORDER BY id DESC"):
            row = dict(r)
            if _pid_alive(row.get("pid")):
                return row
            if row.get("pid") is None:
                # Pre-dates pid tracking, so liveness is unknowable. Leave the
                # row alone rather than declaring a possibly-live crawl dead.
                continue
            # Crawler died without closing its row (killed, crashed, reboot).
            conn.execute(
                "UPDATE linkedin_runs SET status='interrupted' WHERE id=?",
                (row["id"],))
        conn.commit()
    return None


def li_profiles():
    """Tabs fed by the LinkedIn crawler, in profiles.json order."""
    return [k for k, v in load_profiles().items()
            if "linkedin" in scrape.profile_sources(v)]

DEFAULT_LI_KEYWORDS = [
    "Senior Engineering Manager",
    "Director of Engineering",
    "Director of Software Engineering",
    "Head of Engineering",
    "Engineering Manager",
]


def _li_run_start(params, started_at, profile):
    """Open a linkedin_runs row; returns its id."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO linkedin_runs(started_at, status, params, keywords, "
            "profile) VALUES (?, 'running', ?, ?, ?)",
            (started_at, json.dumps(params),
             ", ".join(params.get("keywords") or []), profile))
        conn.commit()
        return cur.lastrowid


def _li_run_finish(run_id, status, rc, started_at, profile):
    """Close the row, folding in whatever the crawler's last progress said."""
    if not run_id:
        return
    finished = _iso_now()
    try:
        secs = int((datetime.fromisoformat(finished)
                    - datetime.fromisoformat(started_at)).total_seconds())
    except (TypeError, ValueError):
        secs = None
    p = _linkedin_progress(profile) or {}
    with db() as conn:
        conn.execute(
            "UPDATE linkedin_runs SET finished_at=?, duration_sec=?, "
            "status=?, rc=?, cards=?, published=?, queries_total=? "
            "WHERE id=?",
            (finished, secs, status, rc, p.get("cards"), p.get("published"),
             p.get("queries_total"), run_id))
        conn.commit()


def _li_runs(profile, limit=10):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM linkedin_runs WHERE profile=? "
            "ORDER BY id DESC LIMIT ?", (profile, limit))]


def _run_linkedin_crawl(cmd, run_id, started_at, profile):
    rc = None
    try:
        with _li_log(profile).open("w") as log:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log,
                                    stderr=subprocess.STDOUT)
            with SCRAPES_LOCK:
                SCRAPES[_li_key(profile)]["pid"] = proc.pid
            # Persist it: the guard reads this back after a server restart.
            with db() as c:
                c.execute("UPDATE linkedin_runs SET pid=? WHERE id=?",
                          (proc.pid, run_id))
                c.commit()
            rc = proc.wait()
        status = "done" if rc == 0 else "error"
    except Exception:
        status = "error"
        raise
    finally:
        with SCRAPES_LOCK:
            SCRAPES[_li_key(profile)].update(
                status=status, finished_at=_iso_now(), rc=rc,
            )
        _li_run_finish(run_id, status, rc, started_at, profile)


def _linkedin_log_tail(profile, lines=40):
    try:
        return _li_log(profile).read_text().strip().splitlines()[-lines:]
    except OSError:
        return []


def _linkedin_progress(profile):
    """Structured progress written by linkedin_scrape.py (phase/pct/ETA)."""
    try:
        return json.loads(_li_progress_path(profile).read_text())
    except (OSError, ValueError):
        return None


@app.route("/api/linkedin/crawl", methods=["POST"])
def api_linkedin_crawl():
    """Kick off linkedin_scrape.py with the options chosen in the UI."""
    body = request.get_json(silent=True) or {}
    profile = str(body.get("profile") or "linkedin")
    if profile not in li_profiles():
        return jsonify({"error": f"{profile!r} is not a LinkedIn-fed tab"}), 400
    # One crawl at a time across all tabs: they share LinkedIn's rate limit,
    # so running two only earns a 429. The message names the busy tab.
    busy = _li_running()
    if busy:
        return jsonify({"started": False, "busy_profile": busy.get("profile"),
                        "status": "running",
                        "started_at": busy.get("started_at")})

    cmd = [sys.executable, str(ROOT / "linkedin_scrape.py"),
           "--profile", profile, "--out", str(li_out_path(profile))]
    tab_cfg = load_profiles().get(profile) or {}
    # A named search brings its own keywords and title filter; --profile above
    # still wins, so a search can be pointed at whichever tab is being crawled.
    search = str(body.get("search") or "").strip()
    searches = linkedin_scrape.load_searches()
    if search and search in searches:
        cmd += ["--search", search]
    else:
        search = ""
    # Use the signed-in endpoint whenever a session exists (boolean queries,
    # sortBy); linkedin_scrape falls back to guest on its own otherwise.
    if body.get("voyager") or body.get("use_tab_defaults") or li_session_status()["signed_in"]:
        cmd.append("--voyager")
    keywords = body.get("keywords")
    if body.get("use_tab_defaults"):
        # "Refresh data" on a mixed tab: the tab's own keywords, remote when
        # the tab asks for it, no manual form involved.
        keywords = tab_cfg.get("linkedin_keywords") or [tab_cfg.get("label") or profile]
        if tab_cfg.get("require_remote_or_local"):
            body["remote"] = True
    if not keywords and not search:
        keywords = tab_cfg.get("linkedin_keywords") or DEFAULT_LI_KEYWORDS
    for kw in (keywords or []):
        kw = str(kw).strip()
        if kw:
            cmd += ["--keywords", kw]
    for url in (body.get("urls") or []):
        url = str(url).strip()
        # Only accept real LinkedIn search URLs — this string reaches a
        # subprocess arg, so don't pass through arbitrary user input.
        if url.startswith("https://www.linkedin.com/"):
            cmd += ["--url", url]
    if body.get("remote"):
        cmd.append("--remote")
    for flag, key in (("--posted-days", "posted_days"),
                      ("--min-pay", "min_pay"),
                      ("--min-fit", "min_fit"),
                      ("--max-per-query", "max_per_query")):
        val = body.get(key)
        if val:
            cmd += [flag, str(int(val))]
    # A LinkedIn tab always filters by its own profile. The keyword queries are
    # deliberately loose (e.g. `adjunct AND cybersecurity`, which the guest
    # endpoint reduces to just `cybersecurity`), and the title filter is what
    # keeps off-target roles out of the tab. Relying on the UI to pass it let a
    # stale page — or a title_profile of "(none)" — publish unfiltered leadership
    # roles into the adjunct tab, so default it to the tab here rather than
    # trusting the client.
    tp = body.get("title_profile") or profile
    if tp and tp in scrape.PROFILES:
        cmd += ["--title-profile", tp]
    if body.get("fresh"):
        cmd.append("--fresh")

    # Remember the form per tab, so each search keeps its own setup (not the
    # implicit Refresh run, which carries no form).
    if not body.get("use_tab_defaults"):
        set_setting(_li_setting_key(profile), body)

    # Drop the previous run's progress so the bar doesn't briefly show a stale
    # 100% before the crawler writes its first update.
    _li_progress_path(profile).unlink(missing_ok=True)

    started_at = _iso_now()
    run_id = _li_run_start(body, started_at, profile)
    with SCRAPES_LOCK:
        SCRAPES[_li_key(profile)] = {"status": "running", "pid": None,
                                     "started_at": started_at,
                                     "run_id": run_id, "profile": profile,
                                     "finished_at": None, "rc": None}
        snapshot = dict(SCRAPES[_li_key(profile)])
    threading.Thread(target=_run_linkedin_crawl,
                     args=(cmd, run_id, started_at, profile),
                     daemon=True).start()
    return jsonify({"started": True, **snapshot})


@app.route("/api/linkedin/crawl", methods=["GET"])
def api_linkedin_crawl_status():
    profile = request.args.get("profile") or "linkedin"
    if profile not in li_profiles():
        abort(404)
    with SCRAPES_LOCK:
        entry = dict(SCRAPES.get(_li_key(profile)) or {"status": "idle"})
        busy = next((k.split(":", 1)[1] for k, v in SCRAPES.items()
                     if k.startswith("_linkedin:")
                     and v.get("status") == "running"), None)
    searches = linkedin_scrape.load_searches()
    entry["profile"] = profile
    # Another tab's crawl blocks this one, so the UI can say which.
    entry["busy_profile"] = busy
    entry["generated"] = _latest_generated(profile)
    entry["log"] = _linkedin_log_tail(profile)
    entry["progress"] = _linkedin_progress(profile)
    entry["default_keywords"] = ((load_profiles().get(profile) or {}).get("linkedin_keywords")
                                or DEFAULT_LI_KEYWORDS)
    entry["session"] = li_session_status()
    entry["searches"] = {
        key: {"label": cfg.get("label", key),
              "profile": cfg.get("profile", "linkedin"),
              "keywords": cfg.get("keywords") or []}
        for key, cfg in sorted(searches.items())}
    # Default the form to whichever search feeds this tab.
    entry["default_search"] = next(
        (k for k, cfg in sorted(searches.items())
         if cfg.get("profile") == profile), "")
    entry["saved"] = _li_saved(profile)
    # Run history survives restarts; `last_run` is the most recent *finished*
    # crawl so the UI can answer "when did this last run, and for how long".
    runs = _li_runs(profile)
    entry["runs"] = runs
    entry["last_run"] = next((r for r in runs if r["status"] != "running"),
                             None)
    return jsonify(entry)


SEEN_ALLOWED_ACTIONS = {"dismissed", "viewed", "saved"}


@app.after_request
def _cors_for_seen(resp):
    # Extension content scripts call these from arbitrary ATS/job domains:
    #   /api/seen*    -> LinkedIn seen tracker (from linkedin.com)
    #   /api/answers* -> field-capture (Learn mode, from any application page)
    if (request.path.startswith("/api/seen") or request.path.startswith("/api/answers")
            or request.path.startswith("/api/profile")):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/seen", methods=["POST", "OPTIONS"])
def api_seen_post():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    linkedin_id = (body.get("linkedin_id") or "").strip()
    hash_ = (body.get("hash") or "").strip()
    if not linkedin_id and not hash_:
        return jsonify({"error": "linkedin_id or hash required"}), 400
    action = body.get("action") or "dismissed"
    if action not in SEEN_ALLOWED_ACTIONS:
        return jsonify({"error": f"action must be one of {sorted(SEEN_ALLOWED_ACTIONS)}"}), 400
    company = (body.get("company") or "").strip()
    title = (body.get("title") or "").strip()
    location = (body.get("location") or "").strip()
    # Fall back to hash as the PK if LinkedIn id is missing (rare).
    pk = linkedin_id or f"hash:{hash_}"
    now = _iso_now()
    with db() as conn:
        conn.execute("""
            INSERT INTO linkedin_seen(linkedin_id, hash, company, title, location,
                                      action, first_seen_at, last_seen_at, seen_count)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(linkedin_id) DO UPDATE SET
                hash = COALESCE(NULLIF(excluded.hash, ''), linkedin_seen.hash),
                company = COALESCE(NULLIF(excluded.company, ''), linkedin_seen.company),
                title = COALESCE(NULLIF(excluded.title, ''), linkedin_seen.title),
                location = COALESCE(NULLIF(excluded.location, ''), linkedin_seen.location),
                action = excluded.action,
                last_seen_at = excluded.last_seen_at,
                seen_count = linkedin_seen.seen_count + 1
        """, (pk, hash_, company, title, location, action, now, now))
        conn.commit()
    return jsonify({"ok": True, "linkedin_id": pk, "action": action})


@app.route("/api/seen/lookup", methods=["POST", "OPTIONS"])
def api_seen_lookup():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    ids = [str(x) for x in (body.get("ids") or []) if x]
    hashes = [str(x) for x in (body.get("hashes") or []) if x]
    out = {}
    if not ids and not hashes:
        return jsonify({"seen": out})
    with db() as conn:
        if ids:
            q = ",".join("?" * len(ids))
            for r in conn.execute(
                f"SELECT linkedin_id, hash, action, first_seen_at, last_seen_at, "
                f"seen_count FROM linkedin_seen WHERE linkedin_id IN ({q})", ids
            ):
                out[r["linkedin_id"]] = dict(r)
        if hashes:
            q = ",".join("?" * len(hashes))
            for r in conn.execute(
                f"SELECT linkedin_id, hash, action, first_seen_at, last_seen_at, "
                f"seen_count FROM linkedin_seen WHERE hash IN ({q})", hashes
            ):
                # Key by hash too so the extension can resolve either way.
                out.setdefault(r["hash"], dict(r))
    return jsonify({"seen": out})


# ---------------------------------------------------------------------------
# Learn mode: capture the fields you type into job applications.
# ---------------------------------------------------------------------------

# Never store these — passwords, uploads, and card-ish field types.
ANSWERS_BLOCKED_TYPES = {"password", "hidden", "file"}
# Skip obviously-volatile / non-answer keys (search boxes, one-off filters).
ANSWERS_BLOCKED_KEY_RE = re.compile(r"\b(search|filter|query|otp|verification code|captcha)\b")


def _normalize_question_key(s: str) -> str:
    """Mirror the extension's client-side normKey so keys merge regardless of
    which label variant (with/without `*`, casing, trailing colon) we get."""
    s = re.sub(r"[∗*]+", " ", s or "")
    s = re.sub(r"\(required\)|\(optional\)", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[:*]+\s*$", "", s).strip()
    return s.lower()[:120]


@app.route("/api/answers", methods=["POST", "OPTIONS"])
def api_answers_post():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    key = _normalize_question_key(body.get("question_key") or "")
    value = (body.get("value") or "").strip()
    field_type = (body.get("field_type") or "text").strip().lower()
    label = (body.get("label") or "").strip()
    domain = (body.get("domain") or "").strip().lower()

    if not key or not value:
        return jsonify({"error": "question_key and value required"}), 400
    if len(key) < 2 or len(value) > 2000:
        return jsonify({"error": "key too short or value too long"}), 400
    if field_type in ANSWERS_BLOCKED_TYPES:
        return jsonify({"skipped": "blocked field type"}), 200
    if ANSWERS_BLOCKED_KEY_RE.search(key):
        return jsonify({"skipped": "blocked key"}), 200

    now = _iso_now()
    with db() as conn:
        row = conn.execute(
            "SELECT variants, domains, seen_count FROM answers WHERE question_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            conn.execute("""
                INSERT INTO answers(question_key, label, field_type, value,
                                    variants, domains, first_seen_at,
                                    last_seen_at, seen_count)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (key, label, field_type, value,
                  json.dumps({value: 1}), json.dumps([domain] if domain else []),
                  now, now))
        else:
            variants = json.loads(row["variants"] or "{}")
            variants[value] = variants.get(value, 0) + 1
            domains = json.loads(row["domains"] or "[]")
            if domain and domain not in domains:
                domains.append(domain)
            conn.execute("""
                UPDATE answers SET label = ?, field_type = ?, value = ?,
                    variants = ?, domains = ?, last_seen_at = ?,
                    seen_count = seen_count + 1
                WHERE question_key = ?
            """, (label or "", field_type, value, json.dumps(variants),
                  json.dumps(domains), now, key))
        conn.commit()
    return jsonify({"ok": True, "question_key": key})


@app.route("/api/answers", methods=["GET"])
def api_answers_get():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM answers ORDER BY seen_count DESC, last_seen_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["variants"] = json.loads(d.get("variants") or "{}")
        d["domains"] = json.loads(d.get("domains") or "[]")
        out.append(d)
    return jsonify({"answers": out, "count": len(out)})


# ------------------------------------------------------------------ glassdoor
#
# The dashboard asks for ratings for the companies it just rendered. Cached
# rows come back immediately; the rest go on a queue that one background worker
# drains slowly (Glassdoor 429s readily, and nothing on the page is waiting on
# it). Refresh the tab a minute later and the column has filled in.

GD_LOCK = threading.Lock()    # guards GD_STATUS and worker startup only
GD_STATUS = {"running": False, "fetched": 0, "error": None}
GD_MAX_REFRESH = 8            # cookie re-extracts allowed per queue drain


def _gd_company_order():
    """Every company across every tab, most recently posted first.

    Glassdoor's throttling puts a hard ceiling on throughput (~1.5 companies a
    minute however fast we ask), so the only lever that matters is order:
    working newest-posting-first means whichever tab you switch to has its
    fresh roles rated, and the long tail of stale postings fills in behind.
    """
    latest = {}
    for path in ROOT.glob(".latest_*.json"):
        try:
            jobs = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for j in jobs or []:
            company = (j.get("company") or "").strip()
            if not company:
                continue
            posted = j.get("posted_at") or ""
            if posted > latest.get(company, ""):
                latest[company] = posted
    return [c for c, _ in sorted(latest.items(), key=lambda kv: kv[1],
                                 reverse=True)]


def _gd_backfill():
    """Queue every not-yet-cached company, newest posting first."""
    companies = _gd_company_order()
    with db() as conn:
        glassdoor.init_db(conn)
        cached = glassdoor.get_cached(conn, companies)
        todo = [c for c in companies if c.strip().lower() not in cached]
        added = glassdoor.queue_add(conn, todo)
    _gd_start_worker()
    return added, len(cached)


def _gd_worker():
    """Drain the queue table one company at a time until it's empty."""
    refreshes = 0
    try:
        while True:
            with db() as conn:
                company = glassdoor.queue_next(conn)
            if not company:
                return
            sess = glassdoor.load_session()
            if not sess:
                with GD_LOCK:
                    GD_STATUS["error"] = ("no session — run "
                                          "tools/gd_session_from_chrome.py")
                return
            try:
                with db() as conn:
                    glassdoor.lookup(conn, company, sess)
                    glassdoor.queue_done(conn, company)
                with GD_LOCK:
                    GD_STATUS["fetched"] += 1
                    GD_STATUS["error"] = None
            except glassdoor.SessionExpired as e:
                # Glassdoor rotates its clearance cookies every 15 minutes or
                # so, which strands the copy we took. Chrome's own jar is still
                # good, so re-copy it and carry on. The company stays queued
                # either way. Bounded per drain so a genuinely dead session
                # doesn't turn into a re-extract loop.
                if refreshes < GD_MAX_REFRESH and glassdoor.refresh_session():
                    refreshes += 1
                    with GD_LOCK:
                        GD_STATUS["error"] = None
                    continue
                with GD_LOCK:
                    GD_STATUS["error"] = f"{e} — re-run tools/gd_session_from_chrome.py"
                return
            except glassdoor.RateLimited as e:
                # Transient, and the company keeps its place in the queue. The
                # next page load or restart starts the worker again.
                with GD_LOCK:
                    GD_STATUS["error"] = str(e)
                return
            except Exception as e:                # one bad company, keep going
                app.logger.warning("glassdoor lookup failed for %s: %s",
                                   company, e)
                with db() as conn:
                    glassdoor.queue_failed(conn, company, e)
    finally:
        with GD_LOCK:
            GD_STATUS["running"] = False


def _gd_start_worker():
    """Start the drain thread unless one is already running."""
    with GD_LOCK:
        if GD_STATUS["running"]:
            return
        GD_STATUS["running"] = True
    threading.Thread(target=_gd_worker, daemon=True).start()


@app.route("/api/glassdoor", methods=["POST"])
def api_glassdoor():
    """Body {companies: [...]}. Returns cached ratings now, fetches the rest."""
    body = request.get_json(force=True, silent=True) or {}
    companies = [c for c in (body.get("companies") or []) if isinstance(c, str)]
    if not companies:
        return jsonify({"ratings": {}, "status": _gd_status()})
    with db() as conn:
        glassdoor.init_db(conn)
        cached = glassdoor.get_cached(conn, companies)
    ratings = {}
    for key, row in cached.items():
        if row["status"] == "ok":
            ratings[key] = {"rating": row["rating"],
                            "reviews": row["review_count"],
                            "url": row["url"],
                            "name": row["employer_name"]}
        else:
            ratings[key] = None            # known miss — don't ask again
    missing = [c for c in companies
               if (c or "").strip().lower() not in cached]
    if missing:
        # The tab being looked at right now outranks the background sweep.
        with db() as conn:
            glassdoor.queue_add(conn, missing, front=True)
        _gd_start_worker()
    return jsonify({"ratings": ratings, "pending": len(missing),
                    "status": _gd_status()})


def _gd_status():
    with db() as conn:
        cached = conn.execute("SELECT COUNT(*) n FROM glassdoor "
                              "WHERE status = 'ok'").fetchone()["n"]
        queued = glassdoor.queue_len(conn)
    with GD_LOCK:
        status = dict(GD_STATUS)
    status.update(cached=cached, queued=queued)
    return status


@app.route("/api/glassdoor", methods=["GET"])
def api_glassdoor_status():
    return jsonify(_gd_status())


@app.route("/api/glassdoor/backfill", methods=["POST"])
def api_glassdoor_backfill():
    """Queue every uncached company in every tab, newest posting first."""
    queued, cached = _gd_backfill()
    return jsonify({"queued": queued, "already_cached": cached,
                    "status": _gd_status()})


@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    # Structured identity + education/work history for the field-capture
    # extension's group filler (education blocks, employment blocks).
    path = Path(__file__).resolve().parent / "profile.json"
    if not path.exists():
        return jsonify({}), 404
    return jsonify(json.loads(path.read_text()))


@app.route("/answers")
def answers_page():
    return render_template("answers.html", profiles=profile_tabs())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    init_db()
    # Start the Glassdoor sweep in the background so ratings keep filling in
    # between visits — it's rate-limited to a trickle and takes many hours, so
    # there's no point waiting for a page load to begin.
    threading.Thread(target=_gd_backfill, daemon=True).start()
    print(f"Serving http://{args.host}:{args.port}/  (state: {DB_PATH})")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
