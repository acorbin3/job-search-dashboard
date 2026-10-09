"""Resume-fit scoring tables ("fit profiles").

A fit profile is the weighted keyword model `scrape.compute_fit()` scores every
job against (0-100). They used to be Python literals; now they are data so the
UI can create one from an uploaded resume (see resume_fit.py).

Storage: fit_profiles.json next to this file (gitignored — it describes a real
person's resume). Shape:

    {
      "_default": "my_resume",            # key scored when no --fit is given
      "my_resume": {
        "label": "Senior SWE resume (2026-05)",
        "level":   [["\\bsenior\\b", 25], ...],          # [regex, points]; first match wins (title)
        "domains": [["\\bpython\\b|\\bgo\\b", 3, 15], ...], # [regex, per-hit, cap] (title+description)
        "sector":  {"healthtech": 8, ...},                 # company_meta sector -> points
        "remote_bonus": 6,
        "location_re": "tampa|florida|\\bfl\\b",           # "" = no location bonus
        "location_bonus": 8,
        "source": "resume.pdf",                            # free-form provenance
        "created": "2026-10-09T12:00:00Z"
      }
    }

`load()` returns the *compiled* form compute_fit expects (tuples with compiled
regexes). `BUILTIN` is a deliberately generic table used only when the file
defines nothing, so a fresh clone scores something sensible out of the box.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIT_FILE = ROOT / "fit_profiles.json"
BUILTIN_KEY = "generic"

# Generic software-engineer table: rewards seniority in the title, common stack
# words, and remote roles. It is intentionally bland — upload a resume to get a
# table that actually reflects you.
BUILTIN = {
    BUILTIN_KEY: {
        "label": "Generic software engineer (built-in)",
        "level": [
            [r"\b(staff|principal)\b", 20],
            [r"\bsenior\b|\bsr\.?\b", 25],
            [r"\b(lead|manager)\b", 14],
            [r"\b(director|head of)\b|\bvp\b|vice president", 8],
            [r"\b(software|backend|back-end|full[ -]?stack|frontend|front-end|platform)\s+(engineer|developer)\b", 14],
        ],
        "domains": [
            [r"\bpython\b|\bjava\b|\bgo(lang)?\b|typescript|\bc#|\.net\b|\bkotlin\b|\brust\b|\bc\+\+", 2, 12],
            [r"kubernetes|\bk8s\b|docker|terraform|\baws\b|\bgcp\b|\bazure\b|ci/cd", 2, 10],
            [r"microservices?|rest(ful)? apis?|graphql|\bgrpc\b|distributed systems?|event.driven|\bkafka\b", 2, 10],
            [r"postgres(ql)?|mysql|\bsql\b|\bredis\b|mongodb|dynamodb|snowflake", 1, 6],
            [r"\breact\b|\bangular\b|\bvue\b|next\.js|frontend|front-end", 1, 6],
            [r"machine learning|\bllm\b|genai|generative ai|pytorch|tensorflow|inference", 1, 6],
        ],
        "sector": {"devtools": 4, "data-infra": 4, "ai-native": 4, "healthtech": 2, "fintech": 2},
        "remote_bonus": 8,
        "location_re": "",
        "location_bonus": 0,
        "source": "built-in",
    }
}

SCHEMA_FIELDS = ("label", "level", "domains", "sector", "remote_bonus",
                 "location_re", "location_bonus")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# JSON <-> compiled

def compile_spec(spec: dict) -> dict:
    """JSON-shaped spec -> the dict compute_fit() consumes. Raises ValueError."""
    err = validate_spec(spec)
    if err:
        raise ValueError(err)
    loc = (spec.get("location_re") or "").strip()
    return {
        "label": spec["label"],
        "level": [(re.compile(rx, re.I), int(pts)) for rx, pts in spec["level"]],
        "domains": [(re.compile(rx, re.I), int(per), int(cap))
                    for rx, per, cap in spec["domains"]],
        "sector": {str(k): int(v) for k, v in (spec.get("sector") or {}).items()},
        "remote_bonus": int(spec.get("remote_bonus", 0)),
        # A never-matching regex keeps compute_fit's unconditional .search() simple.
        "location_re": re.compile(loc if loc else r"(?!x)x", re.I),
        "location_bonus": int(spec.get("location_bonus", 0)) if loc else 0,
        "source": spec.get("source", ""),
        "created": spec.get("created", ""),
    }


def validate_spec(spec) -> str | None:
    """Return an error string, or None when `spec` is a usable JSON fit profile."""
    if not isinstance(spec, dict):
        return "fit profile must be an object"
    if not (spec.get("label") or "").strip():
        return "label is required"
    level = spec.get("level")
    if not isinstance(level, list) or not level:
        return "level needs at least one [regex, points] entry"
    for i, row in enumerate(level):
        if not (isinstance(row, (list, tuple)) and len(row) == 2):
            return f"level[{i}] must be [regex, points]"
        e = _rx_err(row[0])
        if e:
            return f"level[{i}]: {e}"
        if not _is_int(row[1]):
            return f"level[{i}] points must be an integer"
    domains = spec.get("domains")
    if not isinstance(domains, list) or not domains:
        return "domains needs at least one [regex, per_hit, cap] entry"
    for i, row in enumerate(domains):
        if not (isinstance(row, (list, tuple)) and len(row) == 3):
            return f"domains[{i}] must be [regex, per_hit, cap]"
        e = _rx_err(row[0])
        if e:
            return f"domains[{i}]: {e}"
        if not (_is_int(row[1]) and _is_int(row[2])):
            return f"domains[{i}] per_hit and cap must be integers"
    sector = spec.get("sector") or {}
    if not isinstance(sector, dict) or not all(_is_int(v) for v in sector.values()):
        return "sector must map sector name -> integer points"
    for f in ("remote_bonus", "location_bonus"):
        if f in spec and spec[f] is not None and not _is_int(spec[f]):
            return f"{f} must be an integer"
    loc = spec.get("location_re") or ""
    if not isinstance(loc, str):
        return "location_re must be a string"
    e = _rx_err(loc) if loc.strip() else None
    if e:
        return f"location_re: {e}"
    return None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool) or (
        isinstance(v, float) and v.is_integer()) or (
        isinstance(v, str) and v.strip().lstrip("-").isdigit())


def _rx_err(pattern):
    if not isinstance(pattern, str) or not pattern.strip():
        return "empty regex"
    try:
        re.compile(pattern, re.I)
    except re.error as e:
        return f"invalid regex {pattern!r} ({e})"
    return None


def normalize_spec(spec: dict) -> dict:
    """Coerce numbers / strip whitespace so what we save is tidy JSON."""
    out = {
        "label": spec["label"].strip(),
        "level": [[rx.strip(), int(float(p))] for rx, p in spec["level"]],
        "domains": [[rx.strip(), int(float(a)), int(float(b))] for rx, a, b in spec["domains"]],
        "sector": {str(k): int(float(v)) for k, v in (spec.get("sector") or {}).items()},
        "remote_bonus": int(float(spec.get("remote_bonus") or 0)),
        "location_re": (spec.get("location_re") or "").strip(),
        "location_bonus": int(float(spec.get("location_bonus") or 0)),
    }
    for k in ("source", "created", "notes"):
        if spec.get(k):
            out[k] = spec[k]
    return out


# --------------------------------------------------------------------------
# File store

def _read_file() -> dict:
    if not FIT_FILE.exists():
        return {}
    try:
        data = json.loads(FIT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(data: dict) -> None:
    tmp = FIT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(FIT_FILE)


def load_specs() -> dict:
    """{key: JSON spec} for every *user* profile (file only, no built-in)."""
    return {k: v for k, v in _read_file().items()
            if not k.startswith("_") and isinstance(v, dict) and validate_spec(v) is None}


def default_key() -> str:
    """Which profile scores a run with no explicit --fit."""
    data = _read_file()
    specs = load_specs()
    d = data.get("_default")
    if d in specs:
        return d
    if "default" in specs:      # pre-UI installs named their main table "default"
        return "default"
    if specs:
        return next(iter(specs))
    return BUILTIN_KEY


def load() -> dict:
    """{key: compiled profile}. Always includes the built-in as a fallback."""
    out = {k: compile_spec(v) for k, v in BUILTIN.items()}
    for k, v in load_specs().items():
        try:
            out[k] = compile_spec(v)
        except ValueError:
            continue
    return out


def save(key: str, spec: dict, make_default: bool = False) -> dict:
    err = validate_spec(spec)
    if err:
        raise ValueError(err)
    if not re.match(r"^[a-z0-9_]{1,32}$", key):
        raise ValueError("key must be lowercase letters, digits, or underscores")
    data = _read_file()
    spec = normalize_spec(spec)
    spec.setdefault("created", _now())
    data[key] = spec
    if make_default or not data.get("_default"):
        data["_default"] = key
    _write_file(data)
    return spec


def delete(key: str) -> bool:
    data = _read_file()
    if key not in data:
        return False
    del data[key]
    if data.get("_default") == key:
        remaining = [k for k in data if not k.startswith("_")]
        if remaining:
            data["_default"] = remaining[0]
        else:
            data.pop("_default", None)
    _write_file(data)
    return True


def set_default(key: str) -> None:
    data = _read_file()
    if key != BUILTIN_KEY and key not in load_specs():
        raise ValueError(f"unknown fit profile {key!r}")
    data["_default"] = key
    _write_file(data)


def labels() -> dict:
    """{key: label} including the built-in — for <select> menus."""
    out = {k: v["label"] for k, v in BUILTIN.items()}
    out.update({k: v["label"] for k, v in load_specs().items()})
    return out
