from engine.evidence import quote_in_text
from engine.names import match_band, normalize_name
from engine.sources import classify, fetchable
from engine.stages.score import Edu, attribution_score, grade_identity, priority, submission_status
from engine.warwick import classify_education, scan


def test_normalize_name():
    assert normalize_name("H.E. Dr. Ama Mensah-Doe, MBA") == "ama mensah doe"
    assert normalize_name("Prof Sir John Smith OBE") == "john smith"
    assert normalize_name("José Ángel Núñez") == "jose angel nunez"


def test_match_band_is_conservative():
    assert match_band("John Smith", "Ministry of Finance", "Dr John Smith", "Ministry of Finance") == "strong"
    assert match_band("John Smith", "Ministry of Finance", "John Smith", "Acme Ltd") == "weak"
    assert match_band("John Smith", "", "Jane Smith", "") == "none"


def test_department_check():
    assert classify_education("University of Warwick", None, None, "MBA") == "WBS"            # Warwick MBA rule
    assert classify_education("Warwick Business School", None, "MSc Business Analytics") == "WBS"
    assert classify_education("University of Warwick", None, "MSc Management") == "WBS"      # WBS programme
    assert classify_education("University of Warwick", None, None, "BSc", "BSc in Physics") == "OTHER"
    assert classify_education("Warwick Manufacturing Group") == "OTHER"
    assert classify_education("University of Warwick") == "GENERIC"
    assert classify_education("University of Oxford", None, None, "MBA") is None
    assert classify_education("University of Warwick", None, None, "Honorary Doctorate") == "HONORARY"
    # an MBA from elsewhere next to a Warwick mention must not become WBS
    assert classify_education("She holds an MBA from Harvard. She later lectured at Warwick.") != "WBS"


def test_scan_levels():
    assert scan("He holds an MBA from Warwick Business School.").level == "WBS"
    assert scan("She studied economics at the University of Warwick.").level in ("WARWICK", "OTHER_DEPT")
    assert scan("He was a guest of the Earl of Warwick at the castle.").level is None
    assert scan("She is from Warwickshire.").level is None
    assert scan("他毕业于华威大学。").level == "WARWICK"
    assert scan("He received an honorary doctorate from Warwick Business School.").level == "WARWICK"


def test_source_tiers():
    assert classify("https://www.wbs.ac.uk/news/x") == (2, "warwick", True)
    assert classify("https://statehouse.gov.ng/bio")[0] == 1
    assert classify("https://www.worldbank.org/en/about/people/x")[0] == 1
    assert classify("https://en.wikipedia.org/wiki/X")[0] == 7
    assert classify("https://someblog.example.com/post")[0] == 5
    assert not fetchable("https://www.linkedin.com/in/someone")
    assert not fetchable("https://example.gov.xx/report.pdf")


def test_quote_verification():
    page = "Hon. Dr Ama Mensah-Doe is the Minister.\nShe  holds an MBA from Warwick Business School and a BSc."
    assert quote_in_text("She holds an MBA from Warwick Business School", page)
    assert not quote_in_text("She completed a DPhil at Oxford", page)
    assert not quote_in_text("MBA", page)  # too short to count


def test_grading_rules():
    wiki_only = [Edu("WBS", 7, "en.wikipedia.org", False)]
    assert grade_identity(wiki_only)[0] == "UNCERTAIN"
    one = [Edu("WBS", 5, "news.example.com", False)]
    assert grade_identity(one)[0] == "PROBABLE"
    two = one + [Edu("WBS", 1, "example.gov.xx", False)]
    assert grade_identity(two)[0] == "CONFIRMED"
    official = [Edu("WBS", 2, "wbs.ac.uk", True)]
    assert grade_identity(official)[0] == "CONFIRMED"
    other = [Edu("OTHER", 2, "warwick.ac.uk", True)]
    assert grade_identity(other)[0] == "WARWICK-NOT-WBS"
    generic = [Edu("GENERIC", 1, "example.gov.xx", False)]
    assert grade_identity(generic)[0] == "UNCERTAIN"
    conflict = official + [Edu("OTHER", 5, "news.example.com", False)]
    assert grade_identity(conflict)[2]  # flagged for a human


def test_attribution_and_priority():
    assert attribution_score([Edu("WBS", 1, "x.gov.xx", False)]) == 3
    assert attribution_score([Edu("WBS", 5, "news.com", False)]) == 2
    assert attribution_score([Edu("WBS", 2, "wbs.ac.uk", True)]) == 1
    assert attribution_score([Edu("WBS", 7, "en.wikipedia.org", False)]) == 0
    assert priority("CONFIRMED", "ON-LIST", 3) == "P1_COUNT"
    assert priority("CONFIRMED", "ON-LIST", 1) == "P1_FIX"
    assert priority("UNCERTAIN", "ON-LIST", 0) == "P3"
    assert priority("WARWICK-NOT-WBS", "ON-LIST", 3) == "HANDOFF"
    assert submission_status("P1_COUNT", "CONFIRMED") == "Submit"
    assert submission_status("P1_COUNT", "PROBABLE") == "Consider"


def test_two_unknown_sites_do_not_confirm():
    spam = [Edu("WBS", 5, "booking.example.dk", False, known=False),
            Edu("WBS", 5, "mail53.example.br", False, known=False)]
    assert grade_identity(spam)[0] == "PROBABLE"
    mixed = spam[:1] + [Edu("WBS", 5, "bbc.co.uk", False, known=True)]
    assert grade_identity(mixed)[0] == "CONFIRMED"
