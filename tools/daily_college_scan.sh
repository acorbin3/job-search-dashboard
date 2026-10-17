#!/usr/bin/env bash
# launchd entry point: scan college boards, merge into the dashboard, notify.
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
python3 tools/scan_colleges.py --notify >> data/college_scan.log 2>&1
