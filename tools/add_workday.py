#!/usr/bin/env python3
"""
Helper to verify and add a Workday tenant to companies.json.

How to find the right tenant/site for a company:
  1. Open the company's careers page (e.g. https://careers.lockheedmartinjobs.com/)
  2. Open browser devtools, Network tab, filter "myworkdayjobs"
  3. Click "Search jobs" or load any listing page
  4. Find a request like:
       https://lockheedmartin.wd1.myworkdayjobs.com/wday/cxs/lockheedmartin/Careers/jobs
     Tenant = "lockheedmartin", wd = "wd1", site = "Careers"
  5. Run: python3 add_workday.py <slug> <tenant> <wd> <site>

Example:
  python3 add_workday.py lockheedmartin lockheedmartin wd1 Careers
"""
import sys
import json
from pathlib import Path
import requests

ROOT = Path(__file__).parent
CONFIG = ROOT.parent / "data" / "companies.json"

def verify(tenant, wd, site):
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    try:
        r = requests.post(url,
                          json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                          headers={"Accept": "application/json", "Content-Type": "application/json"},
                          timeout=8)
        if r.status_code == 200:
            return r.json().get("total", 0)
    except Exception as e:
        print(f"Error: {e}")
    return 0

def main():
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)
    slug, tenant, wd, site = sys.argv[1:5]
    total = verify(tenant, wd, site)
    if total <= 0:
        print(f"FAIL: 0 jobs found at {tenant}.{wd}.myworkdayjobs.com/{site}")
        print("Double-check the URL in your browser's network tab.")
        sys.exit(1)
    print(f"OK: {total} jobs at {tenant}.{wd}.myworkdayjobs.com/{site}")
    cfg = json.loads(CONFIG.read_text())
    entry = {"slug": slug, "tenant": tenant, "wd": wd, "site": site}
    if any(e.get("slug") == slug for e in cfg["workday"]):
        print(f"Updating existing entry for {slug}")
        cfg["workday"] = [entry if e.get("slug") == slug else e for e in cfg["workday"]]
    else:
        cfg["workday"].append(entry)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    print(f"Added to {CONFIG}")
    print(f"Don't forget to add stage+sector to company_meta.py for slug '{slug}'.")

if __name__ == "__main__":
    main()
