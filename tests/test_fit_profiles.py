import json
import re

import pytest

import fit_profiles
import resume_fit


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(fit_profiles, "FIT_FILE", tmp_path / "fit_profiles.json")
    return tmp_path / "fit_profiles.json"


GOOD = {
    "label": "Test resume",
    "level": [[r"\bsenior\b", 25], [r"\bstaff\b", 18]],
    "domains": [[r"\bpython\b|\bgo\b", 3, 12]],
    "sector": {"healthtech": 8},
    "remote_bonus": 6,
    "location_re": "austin|texas|\\btx\\b",
    "location_bonus": 8,
}


def test_builtin_only_when_empty(tmp_store):
    assert list(fit_profiles.load()) == [fit_profiles.BUILTIN_KEY]
    assert fit_profiles.default_key() == fit_profiles.BUILTIN_KEY


def test_save_load_roundtrip_and_default(tmp_store):
    fit_profiles.save("mine", GOOD)
    assert fit_profiles.default_key() == "mine"        # first saved becomes default
    compiled = fit_profiles.load()["mine"]
    assert compiled["level"][0][1] == 25
    assert compiled["level"][0][0].search("Senior Engineer")
    assert compiled["location_re"].search("Austin, TX")
    data = json.loads(tmp_store.read_text())
    assert data["_default"] == "mine" and data["mine"]["created"]


def test_validate_rejects_bad_regex_and_shape():
    bad = dict(GOOD, level=[["(unclosed", 5]])
    assert "invalid regex" in fit_profiles.validate_spec(bad)
    assert fit_profiles.validate_spec(dict(GOOD, domains=[["x", 1]]))
    assert fit_profiles.validate_spec(dict(GOOD, label=""))
    with pytest.raises(ValueError):
        fit_profiles.save("bad key", GOOD)


def test_delete_moves_default(tmp_store):
    fit_profiles.save("a", GOOD)
    fit_profiles.save("b", GOOD, make_default=True)
    assert fit_profiles.default_key() == "b"
    assert fit_profiles.delete("b")
    assert fit_profiles.default_key() == "a"
    assert not fit_profiles.delete("nope")


def test_empty_location_never_matches(tmp_store):
    fit_profiles.save("nolocation", dict(GOOD, location_re="", location_bonus=8))
    c = fit_profiles.load()["nolocation"]
    assert not c["location_re"].search("anywhere")
    assert c["location_bonus"] == 0


def test_compute_fit_uses_saved_profile(tmp_store):
    import scrape
    fit_profiles.save("mine", GOOD)
    fp = fit_profiles.load()["mine"]
    job = scrape.Job(company="x", ats="greenhouse", title="Senior Python Engineer",
                     location="Austin, TX", url="u", description="python go python",
                     snippet="", ai_hits=[], ai_hit_count=0, remote="remote",
                     sector="healthtech")
    # 25 (senior) + min(4 hits*3, 12)=12 + 8 sector + 6 remote + 8 location
    assert scrape.compute_fit(job, fp) == 59


# --- resume_fit ---------------------------------------------------------------

SENIOR_RESUME = """Jane Doe
Austin, TX | jane@example.com

Experience
Senior Software Engineer, Acme Health  2021 - Present
- Built FHIR and HL7 integrations with Epic EHR for patient data pipelines in Python and Go.
- Ran Kubernetes on AWS with Terraform; CI/CD via GitHub Actions. Postgres, Redis, Kafka.
- Mentored two engineers; remote team across three time zones.
Software Engineer, Widgets Inc  2017 - 2021
- Django REST APIs, React frontend in TypeScript, Docker.
Education
BS Computer Science, State University
""" * 2


def test_heuristic_senior_engineer():
    spec, summary = resume_fit.derive_heuristic(SENIOR_RESUME, label="jane")
    assert fit_profiles.validate_spec(spec) is None
    assert summary["track"] == "engineering"
    assert summary["level"] == "senior"
    assert summary["location"] == "Austin, TX"
    assert spec["level"][0][1] == 25 and re.search(spec["level"][0][0], "Senior Engineer", re.I)
    names = [g["name"] for g in summary["groups"]]
    assert "healthcare" in names and "backend" in names
    assert spec["sector"].get("healthtech")
    assert re.search(spec["location_re"], "Remote - Austin", re.I)
    # only terms the resume mentions make it into the regex
    backend = next(d for d, g in zip(spec["domains"], summary["groups"]) if g["name"] == "backend")
    assert "python" in backend[0] and "ruby" not in backend[0]


def test_heuristic_manager_outranks_old_lead_titles():
    text = ("Senior Software Manager, Big Co 2022-Present\n- Lead 3 teams of 24 engineers.\n" * 2 +
            "Technical Lead, Old Co 2015-2019\nTechnical Lead, Older Co 2012-2015\n" + "filler " * 300)
    _, summary = resume_fit.derive_heuristic(text)
    assert summary["level"] == "senior_manager"


def test_heuristic_product_track():
    text = ("Senior Product Manager, Shop Co\n- Owned product roadmap, PRDs, user research, A/B tests.\n"
            "Product Manager, Startup\n- Backlog, user stories, discovery.\n" * 3)
    spec, summary = resume_fit.derive_heuristic(text)
    assert summary["track"] == "product"
    assert summary["level"] == "senior_pm"
    assert fit_profiles.validate_spec(spec) is None


def test_extract_text_docx_and_tex(tmp_path):
    import zipfile
    buf = tmp_path / "r.docx"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   "<w:document><w:body>" + "".join(
                       f"<w:p><w:r><w:t>Line {i} python kubernetes senior engineer</w:t></w:r></w:p>"
                       for i in range(20)) + "</w:body></w:document>")
    text = resume_fit.extract_text("r.docx", buf.read_bytes())
    assert "Line 3 python" in text and "<w:" not in text
    tex = (r"\documentclass{article}\begin{document}\section{Experience} % comment" + "\n" +
           r"\textbf{Senior Engineer} at \href{http://x}{Acme}, Python \& Go." + "\n") * 10
    text = resume_fit.extract_text("r.tex", tex.encode())
    assert "Senior Engineer" in text and "\\textbf" not in text and "comment" not in text


def test_extract_text_rejects_unknown_and_tiny():
    with pytest.raises(ValueError):
        resume_fit.extract_text("r.xyz", b"x" * 500)
    with pytest.raises(ValueError):
        resume_fit.extract_text("r.txt", b"too short")


def test_spec_from_claude_shape():
    data = {"headline": "Senior backend engineer", "track": "engineering", "level": "senior",
            "domains": [{"name": "backend", "terms": ["python", "rest api", "c#"], "weight": 5},
                        {"name": "cloud", "terms": ["aws"], "weight": 2}],
            "sectors": [{"sector": "healthtech", "points": 8}, {"sector": "bogus", "points": 3}],
            "city": "Austin", "state": "tx", "prefers_remote": True}
    spec = resume_fit._spec_from_claude(data, "")
    assert fit_profiles.validate_spec(spec) is None
    assert spec["label"] == "Senior backend engineer"
    assert spec["domains"][0][1:] == [4, 20] and spec["domains"][1][1:] == [2, 8]
    rx = re.compile(spec["domains"][0][0], re.I)
    assert rx.search("REST API") and rx.search("rest-api") and rx.search("C# developer")
    assert spec["sector"] == {"healthtech": 8}
    assert spec["remote_bonus"] == 8
    assert re.search(spec["location_re"], "Austin, TX", re.I)
