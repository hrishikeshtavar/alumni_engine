"""SCREEN: cheap Warwick test for every person. No LLM calls.

HIT   = WBS or a Warwick MBA is named (Wikidata education or Wikipedia text)
MAYBE = Warwick is named in an education context, or another Warwick department is named
MISS  = no Warwick connection found (reason stored; nothing else kept)
"""

from __future__ import annotations

import json
import logging
import re

from .. import wikidata as wd
from .. import wikipedia
from ..db import now
from ..evidence import add_claim, add_source, link
from ..warwick import RANK, ScanResult, scan
from .ingest import warwick_entities

log = logging.getLogger(__name__)


def _org_words(org: str) -> list[str]:
    stop = {"university", "limited", "group", "company", "school", "the", "and", "of", "for", "ministry",
            "government", "institute", "national", "international"}
    return [w for w in re.findall(r"[A-Za-z]{5,}", org or "") if w.lower() not in stop]


def _structured_level(dept: str) -> str:
    return {"WBS": "WBS", "GENERIC": "WARWICK", "OTHER": "OTHER_DEPT"}[dept]


def _status(level: str | None) -> str:
    if level == "WBS":
        return "HIT"
    if level in ("WARWICK", "OTHER_DEPT"):
        return "MAYBE"
    return "MISS"


def run(conn, http, settings, limit: int | None = None, progress_every: int = 200) -> dict:
    info = warwick_entities(conn, http)
    parts, wbs_qid, warwick_qid = info["parts"], info["wbs"], info["warwick"]
    rows = conn.execute("SELECT * FROM person WHERE screen_status='pending' ORDER BY id LIMIT ?",
                        (limit if limit else -1,)).fetchall()
    counts = {"HIT": 0, "MAYBE": 0, "MISS": 0}
    qid_rows = [r for r in rows if r["qid"]]
    plain_rows = [r for r in rows if not r["qid"]]

    done = 0
    for i in range(0, len(qid_rows), 50):
        batch = qid_rows[i:i + 50]
        ents = wd.entities(http, [r["qid"] for r in batch])
        extra = set()
        for ent in ents.values():
            for e in wd.claim_items(ent, "P27")[:1]:
                extra.add(e["qid"])
            for e in wd.claim_items(ent, "P69"):
                if e["degree"]:
                    extra.add(e["degree"])
        labels = wd.labels_for(http, list(extra)) if extra else {}

        for r in batch:
            ent = ents.get(r["qid"], {})
            name = wd.entity_label(ent) or r["name"]
            structured = []
            for e in wd.claim_items(ent, "P69"):
                if e["qid"] in parts:
                    label = parts[e["qid"]]
                    if e["qid"] == wbs_qid or "business school" in label.lower():
                        dept = "WBS"
                    elif e["qid"] == warwick_qid:
                        dept = "GENERIC"
                    else:
                        dept = "OTHER"
                    structured.append((dept, label, e))
            lang, title = wd.pick_wiki(ent)
            page = None
            if title:
                try:
                    page = wikipedia.page(http, lang, title)
                except Exception as exc:  # noqa: BLE001
                    log.info("wikipedia failed for %s: %s", title, exc)
            result = scan(page["text"]) if page else ScanResult()
            level = result.level
            for dept, _, _ in structured:
                cand = _structured_level(dept)
                if RANK[cand] > RANK[level]:
                    level = cand
            status = _status(level)
            reasons = [f"Wikidata educated at: {lab}" for _, lab, _ in structured]
            if result.level:
                reasons.append(f"Wikipedia ({lang}) text: {result.level}")
            country = r["country"] or next((labels.get(e["qid"], "") for e in wd.claim_items(ent, "P27")[:1]), "")
            links: list[str] = []
            if status != "MISS":
                ent_src = add_source(conn, settings, url=wd.ENTITY_URL.format(r["qid"]), text=None,
                                     title=f"Wikidata {r['qid']}", status="structured")
                link(conn, r["id"], ent_src, "wikidata")
                for dept, label, e in structured:
                    data = {"institution": label, "degree": labels.get(e["degree"], ""), "year": e["end"][:4]}
                    add_claim(conn, person_id=r["id"], claim_type="education", data=data,
                              quote=f"educated at: {label}", source_id=ent_src, origin="wikidata", dept=dept)
                if page:
                    src = add_source(conn, settings, url=page["url"], text=page["text"], title=page["title"])
                    link(conn, r["id"], src, "wikipedia")
                    links = page["links"]
            conn.execute(
                "UPDATE person SET name=?, aliases=?, description=?, country=?, wiki_lang=?, wiki_title=?, "
                "wiki_links=CASE WHEN ?='[]' THEN wiki_links ELSE ? END, screen_status=?, screen_reason=?, "
                "screen_snippet=?, updated_at=? WHERE id=?",
                (name, json.dumps(wd.entity_aliases(ent), ensure_ascii=False),
                 ent.get("descriptions", {}).get("en", {}).get("value", ""), country, lang, title,
                 json.dumps(links), json.dumps(links), status, "; ".join(reasons) or "no Warwick mention found",
                 " | ".join(result.snippets)[:900], now(), r["id"]),
            )
            counts[status] += 1
            done += 1
            if done % progress_every == 0:
                log.info("screened %d/%d (HIT %d, MAYBE %d)", done, len(rows), counts["HIT"], counts["MAYBE"])
        conn.commit()

    # People without a Wikidata ID (CSV lists, previous run): try English Wikipedia by name.
    for r in plain_rows:
        title, page, result = None, None, ScanResult()
        try:
            title = wikipedia.find_title(http, r["name"])
            page = wikipedia.page(http, "en", title) if title else None
        except Exception as exc:  # noqa: BLE001
            log.info("wikipedia lookup failed for %s: %s", r["name"], exc)
        # Guard against namesakes: if we know their organisation, the article must mention it.
        words = _org_words(r["context_org"] or "")
        if page and words and not any(w.lower() in page["text"].lower() for w in words):
            page = None
        if page:
            result = scan(page["text"])
            src = add_source(conn, settings, url=page["url"], text=page["text"], title=page["title"])
            link(conn, r["id"], src, "wikipedia")
        # Names from manual lists are already Warwick leads, so they go forward as at least MAYBE.
        status = "HIT" if result.level == "WBS" else "MAYBE"
        reason = "manual/previous list" + (f"; Wikipedia text: {result.level}" if result.level else "; no Wikipedia match")
        conn.execute(
            "UPDATE person SET wiki_lang=?, wiki_title=?, wiki_links=CASE WHEN ?='[]' THEN wiki_links ELSE ? END, "
            "screen_status=?, screen_reason=?, screen_snippet=?, updated_at=? WHERE id=?",
            ("en" if page else None, title if page else None, json.dumps(page["links"] if page else []),
             json.dumps(page["links"] if page else []), status, reason, " | ".join(result.snippets)[:900],
             now(), r["id"]),
        )
        counts[status] += 1
        conn.commit()
    return counts
