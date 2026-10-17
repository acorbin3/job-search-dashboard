"""WP Job Manager boards on a school's own WordPress site:
  https://<school>/?feed=job_feed  (RSS; items carry content:encoded,
  job_listing:location / job_type / company in the site's own namespace).

config: {"url": "https://<school>/?feed=job_feed"}
Verified against Antioch University and Oakwood University.
"""
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests

from ._common import HEADERS, make_job, title_ok, strip_html, clean

name = "wpjobmanager"
needs_browser = False
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


MARKERS = re.compile(r"post_type=job_listing|/job-listing/|/jobs/feed/?$|wp-job-manager|job_listing", re.I)


def _is_feed(html):
    return bool(html) and "<rss" in html[:2000] and "job_listing" in html


def _feed_ok(feed_url):
    """A ?feed=job_feed URL only counts when WordPress actually serves the feed
    (sites without the plugin 404 or hand back the normal RSS / homepage)."""
    try:
        r = requests.get(feed_url, headers=dict(HEADERS, Accept="*/*"), timeout=20)
        return r.status_code == 200 and _is_feed(r.text)
    except Exception:
        return False


def detect(url, html=""):
    html = html or ""
    if "feed=job_feed" in (url or ""):
        feed = url.split("#")[0]
        if _is_feed(html) or MARKERS.search(html) or _feed_ok(feed):
            return {"url": feed}
        return None
    m = re.search(r"""(?:href|src)=["']([^"']*\?feed=job_feed[^"']*)""", html, re.I)
    if m:
        feed = urljoin(url, m.group(1).replace("&amp;", "&"))
        return {"url": feed} if (MARKERS.search(html) or _feed_ok(feed)) else None
    if url and MARKERS.search(html):
        p = urlsplit(url)
        feed = f"{p.scheme}://{p.netloc}/?feed=job_feed"
        return {"url": feed} if _feed_ok(feed) else None
    return None


def _date(s):
    try:
        return parsedate_to_datetime(clean(s)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _local(it, tag):
    for el in it:
        if el.tag.split("}")[-1] == tag:
            return clean(el.text or "")
    return ""


def fetch(config):
    try:
        try:
            r = requests.get(config["url"], headers=dict(HEADERS, Accept="*/*"), timeout=25)
        except requests.exceptions.SSLError:   # e.g. www.inter.edu omits its intermediate cert
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.get(config["url"], headers=dict(HEADERS, Accept="*/*"), timeout=25, verify=False)
        if r.status_code != 200 or b"<rss" not in r.content[:1000]:
            return []
        root = ET.fromstring(r.content.strip())
        out, seen = [], set()
        for it in root.findall(".//item"):
            title = clean(it.findtext("title") or "")
            link = clean(it.findtext("link") or "")
            if not title or not link or link in seen or not title_ok(title):
                continue
            seen.add(link)
            body = it.findtext(CONTENT) or it.findtext("description") or ""
            out.append(make_job(config, "college_wpjobmanager", title, link,
                                location=_local(it, "location"),
                                description=strip_html(body)[:20000],
                                posted=_date(it.findtext("pubDate") or ""),
                                department=_local(it, "job_type")))
        return out
    except Exception:
        return []
