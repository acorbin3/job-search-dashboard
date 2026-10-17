"""Higher-ed ATS fetchers.

Contract for every module in this package:

    name = "peopleadmin"                       # key used in data/colleges.json
    def detect(url: str, html: str) -> dict | None
        # Given a candidate job-board URL (after redirects) and its HTML, return
        # the config dict the fetcher needs when this ATS is recognised, else None.
        # Config always carries: {"slug", "name", "url"} (filled in by discovery);
        # add anything else the fetcher needs (tenant, site, agency, ...).
    def fetch(config: dict) -> list[scrape.Job]
        # Return postings whose title passes scrape.title_matches(). Never raise;
        # return [] on any failure. Use scrape.UA / strip_html / classify_remote /
        # extract_pay / find_ai_hits exactly like the existing fetchers do.
    needs_browser = False                      # True if Playwright is required

Modules are loaded lazily and registered into scrape.FETCHERS by register().
"""
import logging
from importlib import import_module

log = logging.getLogger(__name__)

MODULES = [
    "peopleadmin", "pageup", "neogov", "interfolio", "oracle", "taleo",
    "successfactors", "interviewexchange", "peoplesoft", "paycor",
    "jazzhr", "taleobe", "breezy", "jobscore", "clearcompany", "isolvedhire",
    "applicantstack", "apscareer", "ukgready", "appone", "applitrack", "silkroad",
    "schoolspring", "myjobsadp", "teamtailor", "trinethire", "rippling",
    "wpjobmanager", "prismhr", "catsone", "ashby", "brightmove", "attrax",
    "eightfold", "avature", "keldair", "jobflows", "trakstar", "powerschool", "winocular",
    "willsub", "jics", "focus", "infor", "nlx", "brassring", "beamery", "exacthire", "rss", "generic",
    # aggregators (one entry each in data/colleges.json under "_aggregators")
    "madgex", "higheredjobs", "herc", "academicjobsonline", "cccregistry",
    "academickeys", "theeduledger", "cra", "ymcareers", "ccjobs", "suny", "sbctc", "ncccs",
    "governmentjobs", "usajobs", "schoolspringnational", "edjoin", "k12jobspot", "talent", "adzuna",
]

_loaded = {}


def load_all():
    for m in MODULES:
        if m in _loaded:
            continue
        try:
            _loaded[m] = import_module(f".{m}", __name__)
        except Exception as e:
            log.debug("college fetcher %s unavailable: %s", m, e)
    return _loaded


def _discipline_filter(fetch):
    """Postings kept on a generic teaching title ("Adjunct Instructor") stay
    only when the department/description names a computing discipline; the
    snippet then shows the matching sentence so the reason is visible."""
    import scrape

    def run(config):
        out = []
        for j in fetch(config):
            if scrape.title_matches(j.title):
                out.append(j)
                continue
            hit = scrape.discipline_hit(j.description, strict=not scrape.ROLE_ADJUNCT_RE.search(j.title))
            if hit:
                j.snippet = f"[{hit[0]}] ...{hit[1]}..."
                out.append(j)
        return out
    run.__name__ = getattr(fetch, "__name__", "fetch")
    return run


def register(fetchers: dict):
    """Add every college fetcher to scrape.FETCHERS (keys prefixed 'college_')."""
    for m, mod in load_all().items():
        # A module that is itself mid-import (fetcher imported before scrape)
        # has no attributes yet; it registers on the next call instead.
        if hasattr(mod, "name") and hasattr(mod, "fetch"):
            fetchers.setdefault(f"college_{mod.name}", _discipline_filter(mod.fetch))
    return fetchers


def detect(url: str, html: str = "") -> dict | None:
    """Classify a job-board URL. Returns config dict with 'ats' set, else None."""
    for m, mod in load_all().items():
        if m == "generic" or not hasattr(mod, "detect"):
            continue
        try:
            cfg = mod.detect(url, html)
        except Exception:
            cfg = None
        if cfg:
            cfg["ats"] = f"college_{mod.name}"
            return cfg
    return None
