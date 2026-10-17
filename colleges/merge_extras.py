#!/usr/bin/env python3
"""Fold the side-channel discoveries into the registry and rebuild colleges.json.

    python3 -m colleges.merge_extras

Reads (whichever exist):
  data/extra_institutions.json  institutions outside IPEDS (DAPIP, edtech, military,
                                extension schools, ...) -> new records with unitid "x_<slug>"
  data/vendor_candidates.json   boards found from the vendor/aggregator side for IPEDS
                                schools whose crawl came up short -> configs added
  data/manual_boards_*.json     hands-on audits ({unitid: {configs}} plus "_systems":
                                {name: {board, unitids}} for shared system boards)
A record's status only ever moves up (none -> generic -> ok); existing configs are kept.
"""
import json
import re

from colleges import registry

DATA = registry.DATA
RANK = {"ok": 2, "generic": 1, "none": 0, "error": 0, None: 0}


def _sig(c):
    return json.dumps({k: v for k, v in c.items() if k not in ("slug", "name", "location", "state", "via")}, sort_keys=True)


def _add_configs(rec, cfgs, source, replace=False):
    """Add configs to a record. replace=True drops what the crawl had (a hands-on
    audit found the crawl's board was wrong) but keeps earlier audit configs."""
    if replace and cfgs:
        rec["configs"] = [c for c in rec.get("configs") or [] if c.get("audited")]
        rec["status"] = "none"
    have = {_sig(c) for c in rec.get("configs") or []}
    new = []
    for c in cfgs:
        c = {k: v for k, v in c.items() if k != "via"}
        if replace:
            c["audited"] = True
        if c.get("ats") and _sig(c) not in have:
            new.append(c)
            have.add(_sig(c))
    real = [c for c in new if c["ats"] != "college_generic"]
    rec["configs"] = (real or []) + [c for c in rec.get("configs") or []] + [c for c in new if c["ats"] == "college_generic"]
    if not rec["configs"]:
        return 0
    status = "ok" if any(c["ats"] != "college_generic" for c in rec["configs"]) else "generic"
    if RANK[status] >= RANK.get(rec.get("status")):
        # (also re-derives status after replace=True on a re-run with nothing new)
        rec["status"] = status
        rec["config"] = rec["configs"][0]
        rec["ats"] = rec["configs"][0]["ats"]
    if not new:
        return 0
    rec.setdefault("sources", [])
    if source not in rec["sources"]:
        rec["sources"].append(source)
    return len(new)


def main():
    reg = registry.load_registry()
    stats = {}
    p = DATA / "extra_institutions.json"
    if p.exists():
        n_new = n_cfg = 0
        for e in json.load(open(p)):
            uid = e.get("ipeds_unitid") or "x_" + re.sub(r"[^a-z0-9]+", "-", e["name"].lower()).strip("-")
            rec = reg.get(uid)
            if not rec:
                rec = {"unitid": uid, "name": e["name"], "city": "", "state": e.get("state", ""),
                       "sector": -1, "homepage": e.get("homepage", ""), "system": "",
                       "kind": e.get("kind"), "notes": e.get("notes", ""),
                       "status": "none", "ats": None, "config": None, "configs": [], "candidates": []}
                reg[uid] = rec
                n_new += 1
            n_cfg += _add_configs(rec, e.get("configs") or [], "extra:" + str(e.get("source")))
        stats["extra_institutions"] = {"new records": n_new, "configs added": n_cfg}
    p = DATA / "vendor_candidates.json"
    if p.exists():
        v = json.load(open(p))
        items = v.items() if isinstance(v, dict) else []
        n_cfg = n_rec = 0
        for uid, e in items:
            if uid.startswith("_") or not isinstance(e, dict) or uid not in reg:
                continue
            k = _add_configs(reg[uid], e.get("configs") or [], "vendor:" + str(e.get("source")))
            n_cfg += k
            n_rec += bool(k)
        stats["vendor_candidates"] = {"schools updated": n_rec, "configs added": n_cfg}
    for p in sorted(DATA.glob("manual_boards_*.json")):
        v = json.load(open(p))
        n_cfg = n_rec = 0
        for uid, e in v.items():
            if uid == "_systems":
                for sysname, se in e.items():
                    if se.get("verified") is False:
                        continue
                    for u in se.get("unitids") or []:
                        if u in reg and se.get("board"):
                            k = _add_configs(reg[u], [se["board"]], "manual-system:" + sysname)
                            n_cfg += k
                            n_rec += bool(k)
                continue
            if uid.startswith("_") or not isinstance(e, dict):
                continue
            if uid not in reg:
                if not e.get("homepage"):      # audit of a school we never had a record for
                    continue
                reg[uid] = {"unitid": uid, "name": e.get("name", uid), "city": e.get("city", ""),
                            "state": e.get("state", ""), "sector": e.get("sector", -1),
                            "homepage": e["homepage"], "system": "", "notes": e.get("notes", ""),
                            "status": "none", "ats": None, "config": None, "configs": [], "candidates": []}
            k = _add_configs(reg[uid], e.get("configs") or [], str(e.get("source") or p.stem), replace=True)
            n_cfg += k
            n_rec += bool(k)
        stats[p.name] = {"schools updated": n_rec, "configs added": n_cfg}
    registry.save_registry(reg)
    out = registry.write_colleges_json(reg)
    from collections import Counter
    print(json.dumps(stats, indent=1))
    print("registry status:", dict(Counter(r.get("status") for r in reg.values())))
    print("wrote", out)


if __name__ == "__main__":
    main()
