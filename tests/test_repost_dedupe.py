"""Repost-collapse keying for the LinkedIn crawl and the Gmail applied sync.

Imports applied_dedupe only (not linkedin_scrape) so this runs without
`requests` installed — dedupe_reposts() is a thin wrapper over repost_key().
"""
import applied_dedupe as d


def _co(a, b):
    return d.repost_key(a, "Engineer")[0] == d.repost_key(b, "Engineer")[0]


def _ti(a, b):
    return d.repost_key("Acme", a)[1] == d.repost_key("Acme", b)[1]


class TestCompanyKey:
    def test_legal_suffixes_collapse(self):
        assert _co("Saab", "Saab, Inc.")
        assert _co("Brain Corp", "Brain Corporation")
        assert _co("Summit Utilities", "Summit Utilities, Inc.")

    def test_distinct_companies_stay_apart(self):
        # H&P is Helmerich & Payne; without the &->and rewrite it keys as "hp".
        assert not _co("H&P", "HP")
        # Descriptive tails are load-bearing here: different firms.
        assert not _co("Integer Holdings Corporation", "Integer Technologies")
        assert not _co("M3", "M3 USA")

    def test_blank_and_junk_never_share_a_key(self):
        # All of these normalize to "", so the raw-string fallback has to keep
        # them distinct or every company-less row collapses into one.
        assert not _co("", "..")
        assert not _co("", "International")
        assert not _co("..", "International")


class TestTitleKey:
    def test_repost_variants_collapse(self):
        assert _ti("Senior Manager of Software Engineering",
                   "Sr Manager of Software Engineering")
        assert _ti("Senior Director of Software Engineering",
                   "Sr. Director of Software Engineering")
        assert _ti("Engineering Manager, Cloud", "Engineering Manager, Cloud (Remote)")
        assert _ti("Engineering Manager (Growth)", "Engineering Manager, Growth")
        assert _ti("Senior AI ML Engineer - Remote", "Senior AI/ML Engineer - Remote")
        assert _ti("Adjunct Faculty - Software Development",
                   "Adjunct Faculty – Software Development")   # en dash

    def test_req_ids_stripped(self):
        assert _ti("Senior Engineering Manager (R0933795)", "Senior Engineering Manager")

    def test_different_roles_stay_apart(self):
        assert not _ti("Engineering Manager", "Director of Engineering")
        assert not _ti("Senior Software Engineer", "Staff Software Engineer")

    def test_unparseable_title_falls_back_to_raw(self):
        # norm_title() returns "" for these markers; two different unknown
        # titles at one company must not merge.
        assert not _ti("(role unspecified in snippet)", "(unknown)")


class TestAppliedPath:
    """The Gmail sync uses find_duplicate(), which keys differently."""

    def test_exact_match_any_age(self):
        idx = [d.index_entry("gmail://msg/a", "DeepHealth",
                             "Director Software Dev Engineer", "2026-07-10T22:45:15Z")]
        kind, _ = d.find_duplicate(idx, "DeepHealth",
                                   "Director, Software Dev Engineer",
                                   "2026-08-13T17:05:45Z")
        assert kind == "exact"          # a repost a month later is still a repeat

    def test_unknown_title_matches_only_inside_the_window(self):
        idx = [d.index_entry("gmail://msg/a", "Omada Health",
                             "(role unspecified in snippet)", "2026-08-13T09:08:56Z")]
        near, _ = d.find_duplicate(idx, "Omada Health", "Some Role",
                                   "2026-08-13T09:09:06Z")
        far, _ = d.find_duplicate(idx, "Omada Health", "Some Role",
                                  "2026-09-20T09:09:06Z")
        assert near == "fuzzy"
        assert far is None

    def test_two_known_roles_at_one_company_are_two_applications(self):
        idx = [d.index_entry("gmail://msg/a", "Amgen", "Director of Engineering",
                             "2026-08-15T23:06:00Z")]
        kind, _ = d.find_duplicate(idx, "Amgen", "Staff Data Engineer",
                                   "2026-08-15T23:06:00Z")
        assert kind is None
