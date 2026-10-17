#!/usr/bin/env python3
"""Daily college scan: refresh only the college job boards, merge into the
dashboard's raw cache, re-derive every tab, and report what is new.

    python3 tools/scan_colleges.py              # scan + merge + report
    python3 tools/scan_colleges.py --notify     # also a macOS notification
    python3 tools/scan_colleges.py --tab adjunct_cs --workers 32

Company boards are left untouched (.raw_scan.json rows whose ats is not
college_* / sector is not higher-ed), so this is much faster than a full
`scrape.py --all` and safe to run on a schedule. New postings (URLs never seen
before) are written to reports/colleges_<date>.md and data/college_history.json.
"""
import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import scrape  # noqa: E402
from scrape import Job, scan, dedup_jobs, score_job, set_superset_globals, apply_profile, profile_source  # noqa: E402

HISTORY = ROOT / "data" / "college_history.json"
REPORTS = ROOT / "reports"


def is_college(d):
    return str(d.get("ats", "")).startswith("college_") or d.get("sector") == "higher-ed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--tab", default="adjunct_cs", help="tab whose rules pick the postings reported as new")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--raw", default=str(ROOT / ".raw_scan.json"))
    ap.add_argument("--colleges", default=str(scrape.COLLEGES_FILE), help="boards file (default data/colleges.json)")
    ap.add_argument("--out-dir", default=str(ROOT), help="where .latest_<tab>.json go (default: project root)")
    args = ap.parse_args()

    t0 = time.time()
    set_superset_globals()
    colleges = json.loads(Path(args.colleges).read_text())
    boards = sum(len(v) for k, v in colleges.items() if not k.startswith("_"))
    print(f"scanning {boards} college boards / aggregators ...", flush=True)
    fresh = dedup_jobs(scan(colleges, max_workers=args.workers))
    for j in fresh:
        j.sector = j.sector or "higher-ed"
        score_job(j)
    print(f"  {len(fresh)} college postings in {time.time()-t0:.0f}s", flush=True)

    raw_path = Path(args.raw)
    old = json.loads(raw_path.read_text()) if raw_path.exists() else []
    merged = [d for d in old if not is_college(d)] + [asdict(j) for j in fresh]
    raw_path.write_text(json.dumps(merged))

    all_jobs = [Job(**d) for d in merged]
    for key, p in scrape.PROFILES.items():
        if profile_source(p) != "ats":
            continue
        jobs = apply_profile(all_jobs, key)
        (Path(args.out_dir) / f".latest_{key}.json").write_text(json.dumps([asdict(j) for j in jobs], indent=2))
        print(f"  {key}: {len(jobs)} jobs", flush=True)

    hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    today = date.today().isoformat()
    picked = apply_profile(fresh, args.tab) if args.tab in scrape.PROFILES else fresh
    new = [j for j in picked if j.url not in hist]
    for j in fresh:
        hist.setdefault(j.url, today)
    HISTORY.write_text(json.dumps(hist))

    REPORTS.mkdir(exist_ok=True)
    rep = REPORTS / f"colleges_{today}.md"
    lines = [f"# New college postings — {today}", "",
             f"{len(new)} new of {len(picked)} matching ({len(fresh)} college postings scanned, {boards} boards).", ""]
    for j in sorted(new, key=lambda j: (-j.fit, j.company)):
        loc = f" — {j.location}" if j.location else ""
        lines.append(f"- **{j.company}**: [{j.title}]({j.url}){loc} (fit {j.fit}, {j.ats.replace('college_', '')})")
    rep.write_text("\n".join(lines) + "\n")
    print(f"\n{len(new)} new postings -> {rep}")
    for l in lines[4:24]:
        print(l)
    if args.notify:
        msg = f"{len(new)} new college postings ({len(picked)} matching)"
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "College job scan"'], check=False)


if __name__ == "__main__":
    main()
