"""Warwick / WBS detection rules and the department check.

These are deterministic so that a model can never decide on its own that a generic
'University of Warwick' mention means WBS.

PROGRAMME CATALOGUE NOTE: WBS_PROGRAMME_KEYWORDS is a first draft. Ask WBS for the full
historical programme list (including old names such as Industrial and Business Studies)
and update it before the real run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Explicit WBS wording.
WBS_EXPLICIT = re.compile(
    r"warwick\s+business\s+school|\bwbs\b|school\s+of\s+industrial\s+and\s+business\s+studies|"
    r"warwick\s+school\s+of\s+business",
    re.I,
)
# Warwick MBA rule (from the existing agent instructions): an MBA / Executive MBA (or a DBA, also run only by WBS) from the
# University of Warwick counts as WBS even if the source does not say "Warwick Business School".
MBA = (r"(?:executive\s+mba|\bmba\b|master\s+of\s+business\s+administration|\bm\.b\.a\.?|\bdba\b|"
       r"doctor(?:ate)?\s+(?:of|in)\s+business\s+administration)")
WARWICK_MBA = re.compile(
    rf"warwick\W{{0,3}}(?:executive\s+)?mba|{MBA}[^.\n;]{{0,90}}warwick|warwick[^.\n;]{{0,60}}{MBA}",
    re.I,
)
MBA_RX = re.compile(MBA, re.I)
MBA_ATTACHED = re.compile(
    r"\s*(?:degree\s+|programme\s+|course\s+|with\s+(?:distinction|merit)\s+)?(?:from|at)\s+(?:the\s+)?"
    r"([^,;.()]{0,70}?)(?=\s+(?:and|in\s+\d|where|before|after|followed|while)\b|[,;.()]|$)", re.I)
OTHER_INSTITUTION = re.compile(r"\b(?:from|at)\s+(?:the\s+)?[A-Z][A-Za-z]+", re.U)


def warwick_mba(text: str) -> bool:
    """An MBA that belongs to Warwick in this sentence. 'an MA from Warwick and an MBA from INSEAD' is not one:
    an MBA that names its own institution counts only if that institution is Warwick."""
    for m in MBA_RX.finditer(text or ""):
        attached = MBA_ATTACHED.match(text[m.end():m.end() + 120])
        if attached and attached.group(1).strip():
            if WARWICK.search(attached.group(1)):
                return True
            continue  # this MBA is from somewhere else
        before = text[max(0, m.start() - 60):m.start()]
        w = None
        for w in WARWICK.finditer(before):
            pass
        if w and not OTHER_INSTITUTION.search(WARWICK.sub(" ", before[w.end():])):
            return True  # 'Warwick MBA', 'graduated from Warwick with an MBA'
        after = text[m.end():m.end() + 90]
        w = WARWICK.search(after)
        if w and not OTHER_INSTITUTION.search(after[:w.start()].replace("University of", "")):
            return True
    return False


# Other Warwick departments: evidence of these alone means Warwick-not-WBS.
OTHER_DEPT = re.compile(
    r"warwick\s+manufacturing\s+group|\bwmg\b|warwick\s+medical\s+school|medical\s+school|"
    r"warwick\s+law\s+school|school\s+of\s+law|centre\s+for\s+education\s+studies|"
    r"department\s+of\s+(?:economics|physics|chemistry|computer\s+science|mathematics|statistics|"
    r"engineering|history|philosophy|politics|sociology|psychology|english|law|life\s+sciences)|"
    r"\b(?:ph\.?\s?d|m\.?sc|b\.?sc|b\.?a|m\.?a|ll\.?b|ll\.?m|m\.?eng|b\.?eng|mphil|doctorate|degree)\b"
    r"[^.\n;]{0,40}\b(?:in|of)\s+(?:economics|physics|chemistry|mathematics|maths|computer\s+science|"
    r"engineering|law|history|philosophy|politics|international\s+relations|sociology|psychology|"
    r"english|medicine|statistics|biology|biochemistry|life\s+sciences)\b",
    re.I,
)
# WBS programme titles (draft catalogue). Only used when Warwick is also named.
WBS_PROGRAMME_KEYWORDS = re.compile(
    r"\bmanagement\b|\bbusiness\b|business\s+analytics|\bfinance\b|financial|accounting|marketing|information\s+systems|"
    r"industrial\s+relations|behavioural\s+(?:and\s+economic\s+)?science|"
    r"operational\s+research|industrial\s+and\s+business\s+studies|business\s+administration|"
    r"doctor\s+of\s+business\s+administration|\bdba\b|global\s+energy|management\s+science|"
    r"organisation(?:al)?\s+studies|business\s+studies|\bstrategy\b|entrepreneurship",
    re.I,
)
WARWICK = re.compile(r"\bwarwick\b(?!shire)", re.I)
HONORARY = re.compile(r"honorary\s+(?:degree|doctor\w*|graduate|fellow\w*|d\.?\s?litt|dsc|llb|lld)|honoris\s+causa", re.I)
# Common renderings of Warwick in non-Latin scripts. Extend as needed.
NON_LATIN_WARWICK = ["华威", "沃里克", "華威", "ウォーリック", "워릭", "وارويك", "ووريك", "Уорик", "Ворик"]
EDU_CONTEXT = re.compile(
    r"\b(?:stud(?:y|ied|ies|ying)|graduat\w*|degree|alumn\w*|educat\w*|ph\.?\s?d|doctor(?:ate|al)|"
    r"m\.?sc|b\.?sc|\bma\b|\bba\b|mba|master'?s?|bachelor'?s?|diploma|certificate|attended|"
    r"school|universit\w+|qualification|programme|program|course|cohort|class\s+of)\b",
    re.I,
)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


@dataclass
class ScanResult:
    level: str | None = None          # 'WBS' | 'WARWICK' | 'OTHER_DEPT' | None
    snippets: list[str] = field(default_factory=list)


def has_warwick(text: str) -> bool:
    return bool(WARWICK.search(text)) or any(v in text for v in NON_LATIN_WARWICK)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def classify_sentence(sentence: str) -> str | None:
    if not has_warwick(sentence):
        return None
    if HONORARY.search(sentence):
        return "WARWICK"  # a lead worth checking, but never a HIT
    if WBS_EXPLICIT.search(sentence) or warwick_mba(sentence):
        return "WBS"
    if OTHER_DEPT.search(sentence):
        return "OTHER_DEPT"
    if EDU_CONTEXT.search(sentence) or any(v in sentence for v in NON_LATIN_WARWICK):
        return "WARWICK"
    return None  # e.g. "Earl of Warwick", "Warwick, Rhode Island"


RANK = {"WBS": 3, "WARWICK": 2, "OTHER_DEPT": 1, None: 0}


def scan(text: str, max_snippets: int = 3) -> ScanResult:
    result = ScanResult()
    for sent in sentences(text):
        level = classify_sentence(sent)
        if level is None:
            continue
        if RANK[level] > RANK[result.level]:
            result.level = level
        if len(result.snippets) < max_snippets:
            result.snippets.append(sent[:300])
    return result


# Subject of a degree, read from the same sentence: "degree in political science", "MSc Management",
# "studied economics at". The subject nearest to the Warwick mention decides the department.
SUBJECT_PATTERNS = [
    re.compile(r"\b(?:degree|diploma|doctorate|bachelor'?s?|master'?s?|ph\.?\s?d|d\.?phil|m\.?phil|m\.?sc|"
               r"b\.?sc|m\.?a\.?|b\.?a\.?|ll\.?b|ll\.?m|b\.?eng|m\.?eng)(?:\s+(?:degree|course|programme))?"
               r"\s+(?:in|of)\s+([A-Za-z][A-Za-z ,&'-]{2,60}?)(?=\s+(?:at|from|with|in\s+\d|and\s+(?:a|an|the|later|then)\b)|[,.;(]|$)",
               re.I),
    re.compile(r"\b(?:MSc|MA|BSc|BA|PhD|MPhil|DPhil|LLM|LLB|MEng|BEng)\s+((?:[A-Z][A-Za-z&'-]+)(?:\s+(?:and|&|of|[A-Z][A-Za-z&'-]+))*)"),
    re.compile(r"\bstud(?:ied|ying)\s+([a-z][a-z ,&'-]{2,50}?)\s+(?:at|in)\b", re.I),
]
NOT_A_SUBJECT = re.compile(r"^(?:the|a|an|warwick|university|uk|england|coventry|\d)\b", re.I)


def degree_subject(sentence: str) -> str | None:
    """The subject of study nearest to the Warwick mention in this sentence, if one is stated."""
    w = WARWICK.search(sentence or "")
    anchor = w.start() if w else 0
    best, dist = None, 10 ** 9
    for rx in SUBJECT_PATTERNS:
        for m in rx.finditer(sentence or ""):
            subj = m.group(1).strip(" ,&-")
            if not subj or NOT_A_SUBJECT.match(subj):
                continue
            d = abs(m.start() - anchor)
            if d < dist:
                best, dist = subj, d
    return best


def department_of_sentence(sentence: str) -> str | None:
    """Department check from ONE sentence: 'WBS' | 'OTHER' | 'GENERIC' | 'HONORARY' | None (no Warwick)."""
    text = sentence or ""
    if not has_warwick(text):
        return None
    if HONORARY.search(text) and not (WBS_EXPLICIT.search(HONORARY.sub(" ", text)) or warwick_mba(text)):
        return "HONORARY"
    if WBS_EXPLICIT.search(text) or warwick_mba(text):
        return "WBS"
    if re.search(r"warwick\s+manufacturing\s+group|\bwmg\b|warwick\s+medical\s+school|medical\s+school|"
                 r"warwick\s+law\s+school|school\s+of\s+law|centre\s+for\s+education\s+studies|"
                 r"department\s+of\s+(?!business)\w+", text, re.I):
        return "OTHER"
    subject = degree_subject(text)
    if subject:
        return "WBS" if WBS_PROGRAMME_KEYWORDS.search(subject) else "OTHER"
    return "GENERIC"


def classify_education(*fields: str | None) -> str | None:
    """Department check over the given text only (kept for callers that pass several fields)."""
    return department_of_sentence(" ".join(f for f in fields if f))
