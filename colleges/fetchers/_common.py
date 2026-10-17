"""Helpers shared by the college fetchers (thin wrappers over scrape.py)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scrape  # noqa: E402
from scrape import Job, UA, strip_html, classify_remote, extract_pay, find_ai_hits  # noqa: E402,F401

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def title_ok(title: str) -> bool:
    """Keep a posting: computing word in the title, or a generic teaching
    title ("Adjunct Instructor") - those are kept only if the description
    names the discipline (see colleges.fetchers.register)."""
    return bool(title) and scrape.college_title_candidate(title)


def make_job(config, ats, title, url, location="", description="", posted="", department=""):
    """Build a scrape.Job for a college posting with all derived fields filled."""
    content = description or ""
    if department and department.lower() not in content[:400].lower():
        content = f"Department: {department}. {content}"
    hits, snippet = find_ai_hits(content)
    if not snippet:
        snippet = re.sub(r"\s+", " ", content)[:300]
    pay_min, pay_max, pay_text = extract_pay(content)
    if posted and len(posted) == 10:
        posted += "T00:00:00Z"
    return Job(
        company=config.get("name") or config.get("slug") or "", ats=ats, title=title.strip(),
        location=location or config.get("location", ""), url=url, ai_hits=hits,
        ai_hit_count=sum(content.lower().count(h) for h in hits), snippet=snippet,
        description=content, posted_at=posted or "", updated_at=posted or "",
        pay_min=pay_min, pay_max=pay_max, pay_text=pay_text,
        remote=("remote" if (scrape.ONLINE_RE.search(f"{title} {location or ''}")
                             or scrape.ONLINE_DESC_RE.search(content))
                else classify_remote(location or "", content)),
        stage="", sector="higher-ed",
    )


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()
