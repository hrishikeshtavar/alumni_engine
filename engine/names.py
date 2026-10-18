"""Name normalisation and conservative matching. Rule: when unsure, do not merge."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

HONORIFICS = {
    "dr", "prof", "professor", "mr", "mrs", "ms", "miss", "mx", "sir", "dame", "lord", "lady", "hon",
    "honourable", "honorable", "rt", "right", "excellency", "alhaji", "alhaja",
    "chief", "engr", "eng", "arch", "barr", "sen", "senator", "amb", "ambassador", "rev", "revd",
    "sheikh", "shaikh", "datuk", "dato", "pehin", "prince", "princess", "baron", "baroness", "the",
}
POSTNOMINALS = {
    "mba", "emba", "phd", "dphil", "dba", "msc", "ma", "ba", "bsc", "llb", "llm", "mphil", "frs",
    "obe", "cbe", "mbe", "kbe", "dbe", "ofr", "con", "gcon", "mon", "fca", "aca", "acca", "cfa", "cpa",
    "fcca", "freng", "frsa", "fba", "mp", "qc", "kc", "jr", "sr", "ii", "iii",
}


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """'H.E. Dr. Ama Mensah-Doe, MBA' -> 'ama mensah doe'."""
    text = strip_accents(name or "").lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"^\s*(?:h\.\s?e\.|rt\.?\s+hon\.?)\s+", " ", text)  # "H.E.", "Rt Hon"
    text = re.sub(r"[.,'’`\"]", " ", text)
    text = re.sub(r"[-_/]", " ", text)
    tokens = [t for t in text.split() if t]
    while len(tokens) > 2 and tokens[0] in HONORIFICS:
        tokens.pop(0)
    while len(tokens) > 2 and tokens[-1] in POSTNOMINALS:
        tokens.pop()
    return " ".join(tokens)


def surname(name: str) -> str:
    tokens = normalize_name(name).split()
    return tokens[-1] if tokens else ""


def name_tokens_in_text(name: str, text: str) -> bool:
    """True if the person's surname (or, for one-word names, the name) appears in the text."""
    sn = surname(name)
    if not sn or len(sn) < 2:
        return False
    hay = strip_accents(text).lower()
    hay = re.sub(r"[-_/]", " ", hay)
    return re.search(rf"\b{re.escape(sn)}\b", hay) is not None


def match_band(name_a: str, org_a: str, name_b: str, org_b: str) -> str:
    """Pairwise match band for records without a shared ID: 'strong' | 'weak' | 'none'."""
    na, nb = normalize_name(name_a), normalize_name(name_b)
    if not na or not nb:
        return "none"
    name_score = fuzz.token_sort_ratio(na, nb)
    if name_score < 90:
        return "none"
    if org_a and org_b and fuzz.token_set_ratio(org_a.lower(), org_b.lower()) >= 85:
        return "strong"
    return "weak"
