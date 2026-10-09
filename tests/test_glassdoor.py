"""Glassdoor resolution + page parsing.

Every case here is a mistake the scraper actually made against live pages:
accepting a same-prefix company as a match, reading a competitor's rating out
of the 'similar companies' rail, and missing records that Next.js had sliced in
half across script tags.
"""

import sqlite3

import pytest

import glassdoor as G


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    G.init_db(c)
    return c


class TestNameMatches:
    def test_slug_forms_of_the_same_company_match(self):
        assert G.name_matches("acadiapharmaceuticals", "Acadia Pharmaceuticals Inc")
        assert G.name_matches("sword-health", "Sword Health")
        assert G.name_matches("8451", "84.51°")
        assert G.name_matches("nice", "NiCE")

    def test_leading_the_survives_slugging(self):
        # 'the' is a word in the display name and glued on in the slug.
        assert G.name_matches("theathletic", "The Athletic")

    def test_different_companies_are_rejected(self):
        assert not G.name_matches("carta", "Cartas na Manga")
        assert not G.name_matches("tempus", "Tempus Wealth")
        assert not G.name_matches("runway-ml", "Runway East")
        assert not G.name_matches("sword-health", "Sword")
        assert not G.name_matches("theathletic", "Athletic (Brazil)")

    def test_empty_never_matches(self):
        assert not G.name_matches("", "Databricks")
        assert not G.name_matches("Databricks", "")


# A page shaped like a real overview page: the target employer's record is cut
# in half by a Next.js chunk boundary, and a competitor rail sits above it
# carrying its own rating.
CHUNKED_PAGE = (
    '<script>self.__next_f.push([1,"{\\"companyList\\":[{\\"employer\\":{\\"id\\":8674,'
    '\\"name\\":\\"GE Aerospace\\",\\"ratings\\":{\\"overallRating\\":4.1}}}]"])</script>'
    '<script>self.__next_f.push([1,"{\\"employerName\\":\\"IQV"])</script>'
    '<script>self.__next_f.push([1,"IA\\",\\"employerId\\":8347,\\"overallRating\\":3.7,'
    '\\"reviewsCount\\":15404,\\"industryRating\\":3.5}"])</script>'
)

HEADER_ONLY_PAGE = (
    '<script>self.__next_f.push([1,"{\\"employer\\":{\\"id\\":8674,\\"name\\":\\"GE Aerospace\\",'
    '\\"ratings\\":{\\"overallRating\\":4.1}},\\"ratings\\":{\\"compensationAndBenefitsRating\\":3.2,'
    '\\"overallRating\\":2.8},\\"shortName\\":\\"84.51°\\",\\"reviewsCount\\":418}"])</script>'
)

LD_PAGE = '''
<script type="application/ld+json">
{"@context": "https://schema.org/", "@type": "EmployerAggregateRating",
 "itemReviewed": {"@type": "Organization", "name": "Databricks"},
 "ratingValue": "4", "ratingCount": "1660"}
</script>
'''


class TestParsing:
    def test_reassembles_record_split_across_script_chunks(self):
        rec = G._parse_embedded_rating(CHUNKED_PAGE, 8347)
        assert rec == {"employer_name": "IQVIA", "rating": 3.7,
                       "review_count": 15404}

    def test_ignores_the_similar_companies_rail(self):
        # GE Aerospace's 4.1 appears first in both pages and must not win.
        assert G._parse_embedded_rating(CHUNKED_PAGE, 8347)["rating"] == 3.7
        assert G._parse_embedded_rating(HEADER_ONLY_PAGE, 992979)["rating"] == 2.8

    def test_falls_back_to_the_name_anchored_header_record(self):
        rec = G._parse_embedded_rating(HEADER_ONLY_PAGE, 992979)
        assert rec["employer_name"] == "84.51°"
        assert rec["review_count"] == 418

    def test_json_ld_block_is_the_last_resort(self):
        assert G._parse_embedded_rating(LD_PAGE, 954734) is None
        rec = G._parse_aggregate_rating(LD_PAGE)
        assert rec == {"employer_name": "Databricks", "rating": 4.0,
                       "review_count": 1660}

    def test_a_page_with_no_employer_yields_nothing(self):
        # An error page carries a stray rating with no identity attached.
        stray = '<script>self.__next_f.push([1,"{\\"overallRating\\":4.1}"])</script>'
        assert G._parse_embedded_rating(stray, 123) is None
        assert G._parse_aggregate_rating(stray) is None


class TestQueue:
    def test_drains_in_the_order_queued(self, conn):
        G.queue_add(conn, ["alpha", "beta", "gamma"])
        drained = []
        while (c := G.queue_next(conn)):
            drained.append(c)
            G.queue_done(conn, c)
        assert drained == ["alpha", "beta", "gamma"]

    def test_front_jumps_the_backfill(self, conn):
        G.queue_add(conn, ["sweep1", "sweep2"])
        G.queue_add(conn, ["viewed1", "viewed2"], front=True)
        assert G.queue_next(conn) == "viewed1"

    def test_requeuing_only_ever_raises_priority(self, conn):
        G.queue_add(conn, ["alpha", "beta"])
        # 'beta' is on the tab being viewed — it should overtake 'alpha'...
        G.queue_add(conn, ["beta"], front=True)
        assert G.queue_next(conn) == "beta"
        assert G.queue_len(conn) == 2          # ...without being duplicated
        # ...and a later sweep must not demote it back.
        G.queue_add(conn, ["beta"])
        assert G.queue_next(conn) == "beta"

    def test_repeated_failures_drop_the_company(self, conn):
        G.queue_add(conn, ["flaky"])
        for _ in range(G.MAX_ATTEMPTS):
            assert G.queue_next(conn) == "flaky"
            G.queue_failed(conn, "flaky", "boom")
        assert G.queue_next(conn) is None      # no longer blocking the head

    def test_queueing_is_case_insensitive(self, conn):
        G.queue_add(conn, ["Databricks"])
        G.queue_add(conn, ["databricks"])
        assert G.queue_len(conn) == 1
        G.queue_done(conn, "DATABRICKS")
        assert G.queue_len(conn) == 0

    def test_blank_companies_are_ignored(self, conn):
        assert G.queue_add(conn, ["", "   ", None]) == 0
        assert G.queue_len(conn) == 0


class TestCacheFreshness:
    def test_ttl_depends_on_status(self):
        old = "2020-01-01T00:00:00+00:00"
        for status in ("ok", "notfound", "empty"):
            assert not G._fresh({"status": status, "fetched_at": old})

    def test_unparseable_timestamp_is_not_fresh(self):
        assert not G._fresh({"status": "ok", "fetched_at": ""})
