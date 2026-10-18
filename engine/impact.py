"""External impact markers: evidence of influence outside academia, required for the Broader impact sheet.

Academic job titles (professor, lecturer, dean, research director) never qualify on their own. A marker is one of:
  - a role on a QS-type list (minister, head of state or government, head of an international organisation or
    central bank, leader or board member of a listed company), at any date;
  - a senior public, business or international-organisation role stated on a page about the person;
  - a major national honour or award stated on a page about the person.
"""

from __future__ import annotations

import json
import re

from .attribution import page_context, person_forms, name_occurrences, has_full_name
from .evidence import read_text
from .warwick import sentences

QS_FAMILIES = ("political", "igo", "central_bank", "business")
SENIOR_ROLE = re.compile(
    r"\b(?:minister|secretary\s+of\s+state|member\s+of\s+parliament|\bMP\b|senator|governor|ambassador|"
    r"high\s+commissioner|prime\s+minister|president\s+of\s+(?:the\s+)?[A-Z]|chief\s+executive|\bCEO\b|chairman|"
    r"chairwoman|chair\s+of\s+the\s+board|non-executive\s+director|managing\s+director|director[- ]general|"
    r"co-?founder|founded|founder|commissioner|permanent\s+secretary|deputy\s+governor|chief\s+(?:financial|"
    r"operating|economist|investment)\s*(?:officer)?|\bCFO\b|\bCOO\b|board\s+member|vice[- ]president|"
    r"head\s+of\s+state|secretary[- ]general|mayor)\b")
ACADEMIC_CONTEXT = re.compile(r"\b(?:university|business\s+school|college|faculty|academic|professor|lecturer|dean|"
                              r"student|school\s+of|research\s+(?:centre|center|institute)|journal|PhD)\b", re.I)
MAJOR_HONOUR = re.compile(
    r"\b(?:knighted|knighthood|appointed\s+(?:a\s+)?dame|damehood|CBE|OBE|KBE|DBE|GBE|MBE|Order\s+of\s+[A-Z]|"
    r"L[ée]gion\s+d['’]honneur|Nobel|Padma\s+(?:Shri|Bhushan|Vibhushan)|national\s+honour|New\s+Year\s+Honours|"
    r"Birthday\s+Honours|Queen['’]s\s+Award|King['’]s\s+Award)\b")
MAX_MARKERS = 3


def markers(conn, person) -> list[str]:
    """Up to three short, sourced markers of external impact for one person (empty list if none)."""
    found: list[str] = []
    for m in conn.execute(
            'SELECT m.role_title, m.role_org, m.start, m."end" AS endd, u.family FROM membership m '
            "JOIN universe u ON u.id=m.universe_id WHERE m.person_id=? AND u.family IN (?,?,?,?)",
            (person["id"], *QS_FAMILIES)):
        span = f"{(m['start'] or '?')[:4]} to {(m['endd'] or 'present')[:4]}"
        found.append(f"QS-type role: {m['role_title']}{', ' + m['role_org'] if m['role_org'] else ''} ({span}; Wikidata)")
    for c in conn.execute(
            "SELECT c.claim_type, c.data, c.quote, s.url FROM claim c JOIN source s ON s.id=c.source_id "
            "WHERE c.person_id=? AND c.claim_type IN ('role','award') AND c.verified=1 AND c.rejected_reason IS NULL",
            (person["id"],)):
        d = json.loads(c["data"] or "{}")
        if c["claim_type"] == "role":
            text = f"{d.get('title') or ''} {d.get('organisation') or ''}"
            if SENIOR_ROLE.search(text) and not ACADEMIC_CONTEXT.search(text):
                found.append(f"Role: {text.strip()} ({c['url']})")
        elif MAJOR_HONOUR.search(f"{d.get('name') or ''} {c['quote'] or ''}"):
            found.append(f"Honour: {d.get('name') or c['quote'][:80]} ({c['url']})")
    if len(found) < MAX_MARKERS:
        found += _page_markers(conn, person)
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out[:MAX_MARKERS]


def _page_markers(conn, person) -> list[str]:
    """Senior roles or honours stated on the person's own Wikipedia article (a page about them, no namesakes)."""
    forms = person_forms(person["name"], person["aliases"])
    out: list[str] = []
    for s in conn.execute(
            "SELECT s.url, s.title, s.text_path, ps.via FROM source s JOIN person_source ps ON ps.source_id=s.id "
            "WHERE ps.person_id=? AND ps.via='wikipedia' AND s.status='ok'", (person["id"],)):
        text = read_text(s["text_path"])
        page = page_context(forms, s["title"], s["url"], text, s["via"], person["wiki_title"])
        if not page.about_person or page.has_namesake:
            continue
        for sent in sentences(text)[:80]:
            if "NAMESAKE" in name_occurrences(sent, forms):
                continue
            if MAJOR_HONOUR.search(sent):
                out.append(f'Honour: "{sent[:140]}" ({s["url"]})')
            elif SENIOR_ROLE.search(sent) and not ACADEMIC_CONTEXT.search(sent):
                out.append(f'Role: "{sent[:140]}" ({s["url"]})')
            if len(out) >= MAX_MARKERS:
                return out
    return out
