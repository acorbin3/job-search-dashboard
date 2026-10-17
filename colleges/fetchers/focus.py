"""Focus School Software job boards (Florida school districts):
  https://<district>.focusschoolsoftware.com/focus/jobs

config: {"url": "https://<district>.focusschoolsoftware.com/focus/jobs/"}
The page is a JS app whose RPC is POST /focus/classes/FocusModule.class.php
(multipart: __call__ = [{"controller":"JobsController","method":...,"args":[...],
"key":<per-session hash from the page's controller map>}], __token__ = the
page's setSessionToken value) with Authorization: Bearer <session_id from the
page> and Referer/Origin/X-Requested-With headers (without those the reply is
empty). getJobs(null x5, ["public"]) -> jobs{external[{id, title (a category:
Instructional / Support / Coach ...), position_title (the real title),
facility_name/city/state, posting_open_date_string, bargaining_unit_title}]};
getJobPosting(id) -> {job, details[{TITLE, DATA (HTML),
__POSITION_CONTROL_PAY_TYPE_ID, __POSITION_CONTROL_HOURS_PER_DAY}]}.
Verified against Okaloosa County School District (123 postings).
"""
import json
import re

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "focus"
needs_browser = False
URL_RE = re.compile(r"https?://([a-z0-9-]+\.focusschoolsoftware\.com)/focus/jobs", re.I)
KEY_RE = re.compile(r'"([^"]{40,})":\{"JobsController"')
TOKEN_RE = re.compile(r"setSessionToken\(`([^`]+)`\)")
SID_RE = re.compile(r"session_id\(\)\s*\{\s*return\s*`([^`]+)`")


def detect(url, html=""):
    m = URL_RE.search(url or "") or URL_RE.search(html or "")
    if not m:
        return None
    return {"url": f"https://{m.group(1).lower()}/focus/jobs/"}


class _Rpc:
    def __init__(self, base):
        self.base = base
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        h = self.s.get(base + "/jobs/", timeout=30).text
        self.key = json.loads('"' + KEY_RE.search(h).group(1) + '"')
        self.token = TOKEN_RE.search(h).group(1)
        sid = SID_RE.search(h)
        self.hdr = {"Accept": "application/x-ndjson", "Authorization": f"Bearer {sid.group(1) if sid else ''}",
                    "Referer": base + "/jobs/", "Origin": base.rsplit("/", 1)[0],
                    "X-Requested-With": "XMLHttpRequest"}

    def call(self, method, args):
        call = [{"controller": "JobsController", "method": method, "args": args, "key": self.key}]
        r = self.s.post(self.base + "/classes/FocusModule.class.php", headers=self.hdr, timeout=60,
                        files={"__call__": (None, json.dumps(call)), "__token__": (None, self.token)})
        result = None
        for line in r.text.splitlines():
            try:
                ev = json.loads(line).get("event") or {}
            except Exception:
                continue
            if ev.get("event") == "__token__":
                self.token = ev.get("data") or self.token
            elif ev.get("event") == "result":
                result = ev.get("data")
        return result


def _body(details):
    """'<TITLE>: <DATA>' sections plus the pay type / hours of the position."""
    parts, extra = [], {}
    for d in details or []:
        if d.get("DATA"):
            parts.append(f"{clean(d.get('TITLE') or '')}: {d['DATA']}")
        for k, label in (("__POSITION_CONTROL_PAY_TYPE_ID", "Pay type"), ("__POSITION_CONTROL_HOURS_PER_DAY", "Hours per day"),
                         ("__POSITION_CONTROL_FULL_TIME_WEEKLY_HOURS", "Weekly hours")):
            if d.get(k):
                extra[label] = d[k]
    return "\n".join([f"{k}: {v}" for k, v in extra.items()] + parts)


def fetch(config):
    try:
        base = config["url"].split("/focus/")[0] + "/focus"
        rpc = _Rpc(base)
        d = rpc.call("getJobs", [None, None, None, None, None, ["public"]]) or {}
        rows = ((d.get("jobs") or {}).get("external") or []) + ((d.get("jobs") or {}).get("internal") or [])
        out, seen = [], set()
        for j in rows:
            jid = str(j.get("id") or "")
            title = clean(j.get("position_title") or j.get("title") or "")
            if not jid or jid in seen or not title_ok(title):
                continue
            seen.add(jid)
            body = ""
            try:
                body = _body((rpc.call("getJobPosting", [int(jid)]) or {}).get("details"))
            except Exception:
                pass
            loc = ", ".join(x for x in (clean(j.get("facility_name") or ""), clean(j.get("facility_city") or ""),
                                        clean(j.get("facility_state") or "")) if x)
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", j.get("posting_open_date_string") or "")
            out.append(make_job(config, "college_focus", title, f"{base}/jobs/?job_id={jid}", location=loc,
                                description=strip_html(body)[:20000],
                                posted=f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else "",
                                department=clean(j.get("title") or j.get("bargaining_unit_title") or "")))
        return out
    except Exception:
        return []
