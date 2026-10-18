"""Who a quote is about, and what it says. Applied to every claim before scoring.

Bug fixes from the 9 October review:
  1. A quote counts for a person only if it names them in full (a given name + surname, or a listed
     alternative name). A surname-only, title + surname or pronoun sentence counts only on a page that is
     about that person and names no one else with the same surname.
  2. Every quote is classed EDUCATION, EMPLOYMENT, HONORARY or OTHER. Only EDUCATION can support WBS
     identity. Working or teaching at WBS is recorded, never counted as study.
  3. The department is read from the quote's own sentence, never from the rest of the page.

Rejected quotes are kept with a reason, so reviewers can see what was thrown out and why:
  NAMESAKE            the sentence or page names a different person with the same surname
  EMPLOYMENT          a staff, teaching or research role at Warwick, not study
  HONORARY            an honorary degree or position
  NEGATED             a negated or unsuccessful attempt ("did not have the grades to get into Warwick")
  DEPT_MISMATCH       study at another Warwick department (counts towards 'Warwick not WBS', not WBS)
  UNSUPPORTED_SOURCE  the quote does not support study at Warwick for this person: it does not name them on a
                      page about someone else, does not name Warwick, or names Warwick without any study
  The Evidence sheet's 'Rejected detail' column says which case applies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from .names import HONORIFICS, POSTNOMINALS, strip_accents
from .sources import is_ai_generated
from .warwick import department_of_sentence, has_warwick

# --------------------------------------------------------------------------------------------
# Quote classes
# --------------------------------------------------------------------------------------------
HONORARY_PHRASE = re.compile(
    r"honorary\s+(?:degree|doctor\w*|graduate|fellow\w*|professor\w*|position|title|award|"
    r"d\.?\s?litt|d\.?\s?sc|ll\.?\s?d|ll\.?\s?b|mba|ma|msc)|honoris\s+causa", re.I)
_DEG = (r"(?:degree|diploma|doctorate|studies|programme|course|qualification|m\.?b\.?a|e?mba|m\.?sc|msc|m\.?a|"
        r"b\.?sc|bsc|b\.?a|ph\.?\s?d|phd|d\.?b\.?a|dba|ll\.?m|llm|ll\.?b|llb|dphil|mphil|master'?s?|bachelor'?s?)")
STUDY_WORDS = re.compile(
    r"\b(?:graduat(?:ed|es|ing)|graduate\s+of|stud(?:ied|ying)\b|alumn(?:us|a|i|ae)\b|\balum\b|"
    r"degree|doctorate|doctoral\s+(?:degree|studies|thesis)|educated\s+at|attended|"
    r"(?:completed|obtained|earned|received|holds?|awarded|gained|took)\b[^.;]{0,60}?\b" + _DEG + r"\b)", re.I)
# Degree abbreviations are case-sensitive, so 'ma' or 'ba' inside ordinary words never match.
DEGREE_ABBR = re.compile(r"\b(?:M\.?B\.?A|EMBA|M\.?Sc|MSc|M\.?A\.|MA|B\.?Sc|BSc|B\.?A\.|BA|Ph\.?\s?D|PhD|"
                         r"D\.?B\.?A|DBA|LL\.?M|LLM|LL\.?B|LLB|DPhil|MPhil)\b")
# 'teaches on the MBA' mentions a degree without anyone studying it.
TEACHING_A_PROGRAMME = re.compile(
    r"\b(?:teach(?:es|ing)?|taught|lectur\w*|director\s+of\s+the|programme\s+director|course\s+director|"
    r"convenor|tutor\w*|supervis\w*|admissions)\b[^.;]{0,50}?\b(?:MBA|EMBA|MSc|MA|BSc|BA|PhD|DBA|"
    r"programme|program|course|students?)\b", re.I)
EMPLOYMENT_WORDS = re.compile(
    r"\b(?:professor|lecturer|reader\s+in|fellow(?:ship)?|teaching\s+assistant|researcher|research\s+(?:associate|"
    r"assistant|fellow|director)|visiting|taught|teach(?:es|ing)?|worked|works|joined|faculty|staff|"
    r"chair\b|director|dean|head\s+of|employed|appointed)\b", re.I)
# Negated or failed attempts ("did not have the grades to get into Warwick"). Checked before EDUCATION.
NEGATION = re.compile(
    r"\b(?:did\s+not|didn['’]t|does\s+not|failed\s+to|rejected|turned\s+down|"
    r"applied\s+to\b[^.;]{0,80}?\bbut\b|offered\s+a\s+place\b[^.;]{0,60}?\bbut\b|instead\s+went\s+to|"
    r"missed\s+out\s+on|not\s+have\s+the\s+grades|was\s+not\s+(?:accepted|admitted|offered)|"
    r"unable\s+to\s+(?:get|gain|secure|attend)|declined\s+(?:a|an|the)\s+(?:place|offer)|"
    r"never\s+(?:studied|attended|graduated|enrolled)|dropped\s+out)", re.I)
NEGATION_WINDOW = 120  # characters either side of the Warwick mention


def is_negated(sentence: str) -> bool:
    text = sentence or ""
    anchors = [m.start() for m in re.finditer(r"warwick|\bwbs\b", text, re.I)]
    for m in NEGATION.finditer(text):
        if not anchors or any(abs(m.start() - a) <= NEGATION_WINDOW for a in anchors):
            return True
    return False


PRESENT_TENSE = re.compile(r"\b(?:is|are|currently|now|serves|serving|holds|remains)\b", re.I)
PAST_TENSE = re.compile(r"\b(?:was|were|former|formerly|previously|until|served|worked|taught|joined)\b", re.I)


def quote_class(sentence: str) -> str:
    """NEGATED | EDUCATION | EMPLOYMENT | HONORARY | OTHER. Negation is checked first; study words win over
    staff words in the same sentence."""
    text = sentence or ""
    if is_negated(text):
        return "NEGATED"
    honorary = bool(HONORARY_PHRASE.search(text))
    rest = HONORARY_PHRASE.sub(" ", text)  # an honorary doctorate is not a doctorate
    degree_mention = bool(DEGREE_ABBR.search(rest)) and not TEACHING_A_PROGRAMME.search(rest)
    if STUDY_WORDS.search(rest) or degree_mention:
        return "EDUCATION"
    if honorary:
        return "HONORARY"
    if EMPLOYMENT_WORDS.search(text):
        return "EMPLOYMENT"
    return "OTHER"


def employment_tense(sentence: str) -> str:
    if PRESENT_TENSE.search(sentence) and not re.search(r"\bformer(?:ly)?\b", sentence, re.I):
        return "current"
    if PAST_TENSE.search(sentence):
        return "former"
    return "unclear"


# --------------------------------------------------------------------------------------------
# Person matching
# --------------------------------------------------------------------------------------------
TITLE_WORDS = {h for h in HONORIFICS} | {
    "minister", "president", "governor", "chairman", "chairwoman", "chairperson", "ceo", "mp", "justice",
    "judge", "general", "colonel", "captain", "deputy", "vice", "secretary", "mayor", "speaker", "senior",
    "junior", "mr", "mrs", "ms", "dr", "prof", "sir", "dame", "rt", "hon", "h", "e", "he", "dean", "director",
    "commissioner", "envoy", "premier", "chancellor", "rector", "provost", "principal", "lecturer", "fellow",
    "associate", "assistant", "emeritus", "honorary", "visiting", "doctor", "madam", "madame", "mister",
}
# Capitalised words that can sit right before a surname without being someone's first name.
STOP_CAPS = {
    "the", "a", "an", "in", "at", "on", "as", "when", "after", "before", "while", "since", "during", "from",
    "with", "by", "for", "of", "and", "but", "then", "later", "also", "both", "former", "this", "that", "these",
    "those", "his", "her", "their", "its", "our", "my", "said", "says", "according", "meanwhile", "however",
    "today", "yesterday", "january", "february", "march", "may", "june", "july", "august",
    "september", "october", "november", "december", "warwick", "business", "school", "university", "college",
    "institute", "department", "ministry", "government", "federal", "national", "state", "royal", "british",
    "nigerian", "ghanaian", "kenyan", "american", "indian", "chinese", "professor", "prof", "dr", "mr", "mrs",
    "ms", "sir", "dame", "lord", "lady", "hon", "minister", "president", "dear", "team", "family", "brother",
    "sister", "father", "mother", "son", "daughter", "wife", "husband", "uncle", "aunt", "cousin", "and",
    "congratulations", "welcome", "meet", "interview", "profile", "about", "photo", "image", "credit",
}
TOKEN_BEFORE = re.compile(r"([A-Za-z][A-Za-z'’\.]*)\s+$")


def _norm(text: str) -> str:
    text = strip_accents(text or "").lower()
    text = re.sub(r"[-_/.,'’`\"()|:]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _words(name: str) -> list[str]:
    """Original-case words of a name with honorifics and post-nominals removed."""
    raw = re.sub(r"\(.*?\)", " ", strip_accents(name or ""))
    raw = re.sub(r"[,]", " ", raw)
    words = [w.strip(".") for w in raw.split() if w.strip(".")]
    while len(words) > 2 and words[0].lower().strip(".") in HONORIFICS | TITLE_WORDS:
        words.pop(0)
    while len(words) > 2 and words[-1].lower().strip(".") in POSTNOMINALS:
        words.pop()
    return words


@dataclass
class PersonForms:
    full_names: set[str] = field(default_factory=set)  # normalised full strings: 'ama mensah doe'
    given: set[str] = field(default_factory=set)       # lower-case given / middle names (2+ letters)
    initials: set[str] = field(default_factory=set)
    surnames: set[str] = field(default_factory=set)    # original-case surname forms: 'Mensah-Doe'


def person_forms(name: str, aliases_json: str | None = None) -> PersonForms:
    forms = PersonForms()
    names = [name]
    try:
        names += [a for a in json.loads(aliases_json or "[]") if isinstance(a, str)]
    except (ValueError, TypeError):
        pass
    for n in names:
        words = _words(n)
        if not words:
            continue
        if len(words) >= 2:
            forms.full_names.add(_norm(" ".join(words)))
        forms.surnames.add(words[-1])
        for w in words[:-1]:
            lw = w.lower()
            if len(lw) >= 2 and lw not in TITLE_WORDS:
                forms.given.add(lw)
                forms.initials.add(lw[0])
            elif len(lw) == 1:
                forms.initials.add(lw)
    return forms


def _surname_pattern(form: str) -> re.Pattern:
    body = re.escape(form).replace(r"\-", r"[-\s]")
    # Not part of a longer hyphenated or joined name ('Nardella' inside 'Surdu-Nardella').
    return re.compile(r"(?<![A-Za-z\-])" + body + r"(?![A-Za-z\-])", re.I)


def _compound_pattern(form: str) -> re.Pattern:
    """The surname joined to another name by a hyphen: a different person's double-barrelled surname."""
    s = re.escape(form)
    return re.compile(r"(?<![A-Za-z\-])(?:[A-Z][A-Za-z]+-" + s + r"|" + s + r"-[A-Z][A-Za-z]+)(?![A-Za-z\-])")


ROLE_WORDS = {
    "executive", "chief", "officer", "founder", "cofounder", "chairman", "chairwoman", "chair", "partner", "head",
    "manager", "economist", "analyst", "editor", "author", "researcher", "scientist", "entrepreneur", "investor",
    "banker", "politician", "diplomat", "leader", "member", "trustee", "governor", "spokesperson", "colleague",
    "co-author", "coauthor", "alumnus", "alumna", "graduate", "student", "candidate", "winner", "nominee",
}


def _occurrence(before: str, forms: PersonForms) -> str:
    """Look at the words right before a surname: FULL (a given name), NAMESAKE (another first name) or SURNAME.

    A capitalised word before the surname is a different person's first name, unless it is a title, a role word,
    a possessive ('Burger King's Hees'), or a middle name that follows one of the person's own given names
    ('Bernardo Vieira Hees')."""
    rest = before
    for _ in range(4):
        m = TOKEN_BEFORE.search(rest)
        if not m:
            return "SURNAME"
        raw = m.group(1)
        if "\n" in rest[m.end(1):]:
            return "SURNAME"  # a line break: the word before belongs to the previous line
        if raw.endswith(".") and len(raw.strip(".")) > 1 and raw.strip(".").lower() not in TITLE_WORDS:
            return "SURNAME"  # end of the previous sentence ('...of Heinz. Hees joined'), not a first name
        if re.search(r"['’]s?$", raw) and not raw.lower().startswith(("o'", "d'")):
            return "SURNAME"  # a possessive: the word before belongs to something else
        tok = raw.strip(".").rstrip("'’")
        low = tok.lower()
        if low in forms.given:
            return "FULL"
        if len(tok) == 1:  # an initial: 'J. Wright'
            if low in forms.initials:
                rest = rest[:m.start()]
                continue
            return "NAMESAKE"
        if low in TITLE_WORDS or low in ROLE_WORDS:
            return "SURNAME"
        if tok[:1].isupper() and low not in STOP_CAPS and not tok.isupper():
            # A middle name: one of the person's given names (or initials) sits just before it.
            prev = rest[:m.start()]
            for _ in range(2):
                m2 = TOKEN_BEFORE.search(prev)
                if not m2:
                    break
                t2 = m2.group(1).strip(".").rstrip("'’").lower()
                if t2 in forms.given:
                    return "FULL"
                if len(t2) == 1 and t2 in forms.initials:
                    prev = prev[:m2.start()]
                    continue
                if m2.group(1)[:1].isupper() and t2 not in STOP_CAPS and t2 not in TITLE_WORDS:
                    prev = prev[:m2.start()]  # a second middle name
                    continue
                break
            return "NAMESAKE"
        return "SURNAME"
    return "SURNAME"


def name_mentions(text: str, forms: PersonForms) -> list[tuple[int, str]]:
    """(position, FULL | SURNAME | NAMESAKE) for every mention of the surname, in page order."""
    plain = strip_accents(text or "")
    out: list[tuple[int, str]] = []
    own = {s.lower() for s in forms.surnames}
    for form in forms.surnames:
        for m in _surname_pattern(form).finditer(plain):
            if not plain[m.start()].isupper():
                continue  # 'parker' or 'fix' as an ordinary word
            kind = _occurrence(plain[:m.start()], forms)
            nxt = re.match(r"\s+([A-Z][a-z][A-Za-z'’-]+)\b", plain[m.end():m.end() + 40])
            if kind == "SURNAME" and nxt:
                low = nxt.group(1).lower()
                if low not in STOP_CAPS and low not in TITLE_WORDS and low not in ROLE_WORDS \
                        and low not in forms.given and not plain[m.end():m.end() + 1].strip():
                    kind = "NAMESAKE"  # the 'surname' is someone's first name: 'Alexander Montag'
            out.append((m.start(), kind))
        for m in _compound_pattern(form).finditer(plain):
            if m.group(0).lower() not in own:
                out.append((m.start(), "NAMESAKE"))
    return sorted(out)


def name_occurrences(text: str, forms: PersonForms) -> list[str]:
    return [kind for _, kind in name_mentions(text, forms)]


def has_full_name(text: str, forms: PersonForms) -> bool:
    norm = f" {_norm(text)} "
    if any(f" {fn} " in norm for fn in forms.full_names if " " in fn):
        return True
    # Citation form 'Einy, Rina L.' (surname, comma, a given name): library and thesis records.
    plain = strip_accents(text or "")
    for s in forms.surnames:
        for m in re.finditer(r"(?<![A-Za-z])" + re.escape(s) + r",\s+([A-Z][A-Za-z'-]+)", plain):
            if m.group(1).lower() in forms.given:
                return True
    return False


@dataclass
class PageContext:
    about_person: bool
    has_namesake: bool
    text: str = ""
    mentions: list = field(default_factory=list)
    inferred: bool = False  # 'about the person' only because the full name appears and no namesake does

    def referent_at(self, sentence: str) -> str | None:
        """Who the page was last talking about just before this sentence: 'PERSON', 'OTHER' or None.

        Walks the surname mentions in page order. A full-name mention sets the referent to the person, a
        namesake ('Douglas Alexander', 'Ben Leadsom') to someone else, and a bare surname keeps whoever was
        named last (on the person's own page, a bare surname means the person). A page about the person starts with the person as referent."""
        plain = strip_accents(self.text or "")
        key = strip_accents(sentence or "")[:80]
        pos = plain.find(key) if key else -1
        if pos < 0:
            return None
        ref = "PERSON" if self.about_person else None
        for p, kind in self.mentions:
            if p >= pos:
                break
            if kind == "FULL" or (kind == "SURNAME" and self.about_person):
                ref = "PERSON"  # on the person's own page, a bare 'Leadsom' means them
            elif kind == "NAMESAKE":
                ref = "OTHER"
        return ref


def page_context(forms: PersonForms, title: str | None, url: str | None, text: str, via: str | None,
                 wiki_title: str | None) -> PageContext:
    title_n = f" {_norm(title or '')} "
    slug = _norm(unquote(urlparse(url or "").path))
    about = False
    if via == "wikipedia" or (wiki_title and wiki_title.replace(" ", "_") in (url or "")):
        about = True
    for fn in forms.full_names:
        if f" {fn} " in title_n or (fn and fn in slug):
            about = True
    if not about and forms.given:
        for s in forms.surnames:
            sn = _norm(s)
            if sn and f" {sn} " in title_n and any(f" {g} " in title_n for g in forms.given):
                about = True
    mentions = name_mentions(text, forms)
    inferred = False
    if not about and any(k == "FULL" for _, k in mentions) and not any(k == "NAMESAKE" for _, k in mentions):
        # The full name appears on the page and no one else with the surname does: 'Dr McKeown' later on
        # a news page that opened with 'Max McKeown' is the same person. Pronoun-only sentences still need
        # a page that is about the person.
        about = inferred = True
    return PageContext(about_person=about, has_namesake=any(k == "NAMESAKE" for _, k in mentions),
                       text=text, mentions=mentions, inferred=inferred)


def person_verdict(sentence: str, forms: PersonForms, page: PageContext) -> str | None:
    """None if the sentence can be about this person, else a rejection reason."""
    occ = name_occurrences(sentence, forms)
    if "NAMESAKE" in occ:
        return "NAMESAKE"
    if "FULL" in occ or has_full_name(sentence, forms):
        return None
    # Surname only, title + surname, a pronoun, or no name at all.
    if page.inferred and "SURNAME" not in occ:
        return "NOT_ABOUT_PERSON"
    if page.about_person and not page.has_namesake:
        return None
    if page.about_person and page.has_namesake:
        # A relative or namesake is named somewhere on the person's own page. Accept the sentence only if
        # the last person named before it is the candidate ('She studied at Warwick' in Andrea Leadsom's
        # article, not a sentence that follows 'her husband Ben Leadsom').
        return None if page.referent_at(sentence) == "PERSON" else "NAMESAKE"
    return "NAMESAKE" if page.has_namesake else "NOT_ABOUT_PERSON"


# --------------------------------------------------------------------------------------------
# Applying it to the claims table
# --------------------------------------------------------------------------------------------
# The six reasons shown to reviewers, with a plain-English detail for each internal case.
REASON_DETAIL = {
    "NAMESAKE": "Names a different person with the same surname",
    "NOT_ABOUT_PERSON": "Sentence does not name the person and the page is not about them",
    "EMPLOYMENT": "Staff, teaching or research role at Warwick, not study",
    "HONORARY": "Honorary degree or position, not study",
    "NEGATED": "Negated or unsuccessful attempt to study at Warwick",
    "NOT_STUDY": "Names Warwick but says nothing about study",
    "NOT_WARWICK": "The quoted sentence does not name Warwick",
    "DEPT_MISMATCH": "Study at another Warwick department (counts towards Warwick not WBS)",
    "AI_GENERATED": "AI-generated encyclopedia page, not an independent source",
}
PUBLIC_REASON = {"NAMESAKE": "NAMESAKE", "NOT_ABOUT_PERSON": "UNSUPPORTED_SOURCE", "EMPLOYMENT": "EMPLOYMENT",
                 "HONORARY": "HONORARY", "NEGATED": "NEGATED", "NOT_STUDY": "UNSUPPORTED_SOURCE",
                 "NOT_WARWICK": "UNSUPPORTED_SOURCE", "DEPT_MISMATCH": "DEPT_MISMATCH",
                 "AI_GENERATED": "UNSUPPORTED_SOURCE"}
PUBLIC_REASONS = ("NAMESAKE", "EMPLOYMENT", "HONORARY", "NEGATED", "DEPT_MISMATCH", "UNSUPPORTED_SOURCE")


def classify_claim(claim_type: str, origin: str, quote: str, dept_stored: str | None,
                   verdict: str | None) -> tuple[str, str | None, str | None]:
    """Return (claim_class, dept, internal reason or None)."""
    if claim_type != "education":
        return claim_type.upper(), dept_stored, verdict
    if origin == "wikidata":
        # A structured 'educated at' statement about this exact Wikidata item. Wikipedia/Wikidata are
        # tier 7, so on their own they can never confirm anyone.
        return "EDUCATION", dept_stored, ("HONORARY" if dept_stored == "HONORARY" else None)
    cls = quote_class(quote)
    if not has_warwick(quote or ""):
        return cls, None, verdict or "NOT_WARWICK"
    dept = department_of_sentence(quote)
    if cls == "NEGATED" and verdict != "AI_GENERATED":
        return cls, dept, "NEGATED"  # rejected either way; 'did not get into Warwick' is the useful reason
    if verdict:
        return cls, dept, verdict
    if cls == "EMPLOYMENT":
        return cls, dept, "EMPLOYMENT"
    if cls == "HONORARY" or dept == "HONORARY":
        return "HONORARY", dept, "HONORARY"
    if cls == "OTHER":
        return cls, dept, "NOT_STUDY"
    if dept == "OTHER":
        return cls, dept, "DEPT_MISMATCH"
    return cls, dept, None


def validate_claims(conn, person_id: int | None = None, force: bool = False) -> dict:
    """Set claim_class, dept and rejected_reason on claims. force=True re-checks everything."""
    where = []
    args: list = []
    if person_id is not None:
        where.append("c.person_id=?")
        args.append(person_id)
    if not force:
        where.append("c.claim_class IS NULL")
    sql = ("SELECT c.id, c.person_id, c.claim_type, c.origin, c.quote, c.dept, c.source_id, s.url, s.title, "
           "s.text_path, ps.via, p.name, p.aliases, p.wiki_title FROM claim c "
           "JOIN person p ON p.id=c.person_id LEFT JOIN source s ON s.id=c.source_id "
           "LEFT JOIN person_source ps ON ps.person_id=c.person_id AND ps.source_id=c.source_id"
           + (" WHERE " + " AND ".join(where) if where else ""))
    rows = conn.execute(sql, args).fetchall()
    forms_cache: dict[int, PersonForms] = {}
    page_cache: dict[tuple[int, int], PageContext] = {}
    texts: dict[int, str] = {}
    stats: dict[str, int] = {}
    for r in rows:
        forms = forms_cache.get(r["person_id"])
        if forms is None:
            forms = forms_cache[r["person_id"]] = person_forms(r["name"], r["aliases"])
        verdict = None
        if r["url"] and is_ai_generated(r["url"]):
            verdict = "AI_GENERATED"
        elif r["origin"] != "wikidata":
            key = (r["person_id"], r["source_id"])
            page = page_cache.get(key)
            if page is None:
                if r["source_id"] not in texts:
                    try:
                        with open(r["text_path"], encoding="utf-8") as fh:
                            texts[r["source_id"]] = fh.read()
                    except (OSError, TypeError):
                        texts[r["source_id"]] = ""
                page = page_cache[key] = page_context(forms, r["title"], r["url"], texts[r["source_id"]],
                                                      r["via"], r["wiki_title"])
            verdict = person_verdict(r["quote"] or "", forms, page)
        cls, dept, internal = classify_claim(r["claim_type"], r["origin"] or "", r["quote"] or "", r["dept"], verdict)
        reason = PUBLIC_REASON.get(internal) if internal else None
        detail = REASON_DETAIL.get(internal) if internal else None
        conn.execute("UPDATE claim SET claim_class=?, dept=?, rejected_reason=?, rejected_detail=? WHERE id=?",
                     (cls, dept, reason, detail, r["id"]))
        stats[reason or "COUNTED"] = stats.get(reason or "COUNTED", 0) + 1
    conn.commit()
    return stats


def staff_flags(conn, person_id: int) -> tuple[str, str]:
    """(Current WBS employee, Former WBS staff) from EMPLOYMENT quotes that are about this person.
    Values: 'Yes', 'No', 'Check (tense unclear)', with '(Warwick, department not stated)' when WBS is not named."""
    rows = conn.execute(
        "SELECT quote, dept FROM claim WHERE person_id=? AND claim_type='education' AND verified=1 "
        "AND claim_class='EMPLOYMENT' AND rejected_reason='EMPLOYMENT'", (person_id,)).fetchall()
    wbs = [r for r in rows if r["dept"] == "WBS"]
    pool = wbs or rows
    if not pool:
        return "No", "No"
    where = "" if wbs else " (Warwick, department not stated)"
    tenses = {employment_tense(r["quote"] or "") for r in pool}
    current = "Yes" + where if "current" in tenses else "No"
    former = "Yes" + where if "former" in tenses else "No"
    if current == "No" and former == "No":
        current = former = "Check (tense unclear)" + where
    return current, former


def employment_status(conn, person_id: int) -> str:
    """Kept for older callers: the Current WBS employee value."""
    return staff_flags(conn, person_id)[0]
