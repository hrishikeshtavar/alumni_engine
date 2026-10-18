"""EXTRACT: turn stored page text into claims, each with a verbatim quote that code checks.

Two extractors run on every linked page:
  rule  - deterministic: sentences that name Warwick about this person become education claims.
  llm   - (if configured) structured education, role and award claims via the free-tier model.
A model claim whose quote is not found in the stored page is kept as verified=0 and never used.
"""

from __future__ import annotations

import logging
import re

from ..attribution import validate_claims
from ..db import PROCESS_ORDER, now
from ..evidence import add_claim, quote_in_text, read_text
from ..llm import EXTRACT_PROMPT, DailyCapReached, LLM
from ..names import name_tokens_in_text, surname
from ..warwick import EDU_CONTEXT, classify_education, classify_sentence, has_warwick, sentences

log = logging.getLogger(__name__)
PRONOUN_START = re.compile(r"^(?:he|she|they|his|her|their|the\s+(?:minister|governor|president|ceo|chair))\b", re.I)
ROLE_WORDS = re.compile(r"\b(minister|president|governor|chair|chief|director|ceo|head|secretary|founder|"
                        r"board|commissioner|ambassador|dean|professor|appointed|served|award|honour|prize)\b", re.I)


def focus_text(text: str, name: str, cap: int = 12000) -> str:
    if len(text) <= cap:
        return text
    sn = surname(name)
    paras = [p for p in text.split("\n") if p.strip()]
    keep = paras[:2]
    for p in paras[2:]:
        low = p.lower()
        if (sn and sn in low) or has_warwick(p) or EDU_CONTEXT.search(p) or ROLE_WORDS.search(p):
            keep.append(p)
    out = "\n".join(keep)
    return out[:cap]


def rule_claims(conn, person, source_id: int, text: str, own_article: bool) -> int:
    sents = sentences(text)
    n = 0
    for i, s in enumerate(sents):
        level = classify_sentence(s)
        if level is None:
            continue
        about = own_article or name_tokens_in_text(person["name"], s)
        if not about and PRONOUN_START.match(s):
            about = any(name_tokens_in_text(person["name"], prev) for prev in sents[max(0, i - 2):i])
        if not about:
            continue
        dept = classify_education(s)
        if dept is None:
            continue
        add_claim(conn, person_id=person["id"], claim_type="education", data={"institution": "Warwick (stated in text)"},
                  quote=s, source_id=source_id, origin="rule", dept=dept)
        n += 1
    return n


def _clean(v):
    return v.strip() if isinstance(v, str) and v.strip() and v.strip().lower() != "null" else None


def llm_claims(conn, llm: LLM, person, src, text: str, context: str) -> dict:
    prompt = EXTRACT_PROMPT.format(name=person["name"], context=context or "none", url=src["url"],
                                   text=focus_text(text, person["name"]))
    res = llm.json(prompt)
    stats = {"kept": 0, "dropped": 0}
    if res.get("about_this_person") is False:
        return stats
    pub = _clean(res.get("publication_date"))
    if pub and re.match(r"^\d{4}(-\d{2}(-\d{2})?)?$", pub) and not src["published_at"]:
        conn.execute("UPDATE source SET published_at=? WHERE id=?", (pub, src["id"]))

    def store(kind: str, item: dict, data: dict, dept: str | None):
        quote = _clean(item.get("quote")) or ""
        ok = quote_in_text(quote, text)
        add_claim(conn, person_id=person["id"], claim_type=kind, data=data, quote=quote, source_id=src["id"],
                  origin="llm", dept=dept, verified=ok)
        stats["kept" if ok else "dropped"] += 1

    for e in res.get("education") or []:
        if not isinstance(e, dict):
            continue
        data = {k: _clean(e.get(k)) for k in ("institution", "school_or_department", "programme", "degree", "year")}
        dept = classify_education(data["institution"], data["school_or_department"], data["programme"],
                                  data["degree"], e.get("quote"))
        store("education", e, data, dept)
    for r in res.get("roles") or []:
        if isinstance(r, dict) and _clean(r.get("title")):
            data = {k: _clean(r.get(k)) for k in ("title", "organisation", "start", "end")}
            data["is_current"] = r.get("is_current") if isinstance(r.get("is_current"), bool) else None
            store("role", r, data, None)
    for a in res.get("awards") or []:
        if isinstance(a, dict) and _clean(a.get("name")):
            store("award", a, {"name": _clean(a.get("name")), "year": _clean(a.get("year"))}, None)
    return stats


def run(conn, http, settings, limit: int | None = None) -> dict:
    llm = LLM(conn, http, settings)
    rows = conn.execute(
        "SELECT * FROM person WHERE fetch_status='done' AND extract_status='pending' "
        + PROCESS_ORDER + " LIMIT ?",
        (limit if limit else -1,),
    ).fetchall()
    stats = {"people": 0, "rule_claims": 0, "llm_kept": 0, "llm_dropped": 0, "stopped": "",
             "model": llm.describe(), "model_calls": 0}
    for p in rows:
        roles = conn.execute("SELECT role_title, role_org FROM membership WHERE person_id=? AND role_title!='Educated at'",
                             (p["id"],)).fetchall()
        context = "; ".join(f"{r['role_title']} {r['role_org']}".strip() for r in roles[:5]) or (p["context_org"] or "")
        srcs = conn.execute(
            "SELECT s.*, ps.via FROM source s JOIN person_source ps ON ps.source_id=s.id "
            "WHERE ps.person_id=? AND s.status='ok' AND s.text_path IS NOT NULL", (p["id"],)).fetchall()
        for s in srcs:
            text = read_text(s["text_path"])
            if not text:
                continue
            stats["rule_claims"] += rule_claims(conn, p, s["id"], text, own_article=(s["via"] == "wikipedia"))
            # Save free-tier calls: Wikidata already gives roles for people with a QID.
            if llm.available and not (s["tier"] == 7 and p["qid"]):
                try:
                    st = llm_claims(conn, llm, p, s, text, context)
                    stats["llm_kept"] += st["kept"]
                    stats["llm_dropped"] += st["dropped"]
                except DailyCapReached as exc:
                    # Keep going with rule-based extraction; the model's claims are an extra, not the proof.
                    llm.available = False
                    stats["stopped"] = f"{exc}; continued with rule-based extraction only"
                    log.warning("%s; continuing with rule-based extraction only", exc)
        validate_claims(conn, p["id"], force=True)
        conn.execute("UPDATE person SET extract_status='done', updated_at=? WHERE id=?", (now(), p["id"]))
        conn.commit()
        stats["people"] += 1
    stats["model_calls"] = llm.calls_made
    return stats
