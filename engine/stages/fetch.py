"""FETCH: for HIT and MAYBE people only, collect a few independent pages and store their text.

Candidate pages come from (1) the references cited by the person's Wikipedia article and
(2) optional free-tier web search. Pages that do not mention the person's surname are discarded.
"""

from __future__ import annotations

import json
import logging

from .. import pages
from ..db import PROCESS_ORDER, now
from ..evidence import add_source, link, read_text
from ..names import name_tokens_in_text, surname
from ..search import Search, SearchBudgetExhausted
from ..sources import classify, domain_of, fetchable

log = logging.getLogger(__name__)


def rank_urls(urls: list[tuple[str, str]], name: str, max_pages: int) -> list[tuple[str, str]]:
    sn = surname(name)
    scored, seen_urls = [], set()
    for url, via in urls:
        if not fetchable(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        tier, _, warwick_side = classify(url)
        score = (0 if warwick_side else 1, tier, 0 if sn and sn in url.lower() else 1)
        scored.append((score, url, via))
    scored.sort()
    out, per_domain = [], {}
    for _, url, via in scored:
        d = domain_of(url)
        if per_domain.get(d, 0) >= 2:
            continue
        per_domain[d] = per_domain.get(d, 0) + 1
        out.append((url, via))
        if len(out) >= max_pages:
            break
    return out


def search_queries(person) -> list[str]:
    name = person["name"]
    qs = [f'"{name}" "Warwick Business School" OR "Warwick MBA" OR "University of Warwick"']
    if person["screen_status"] == "MAYBE" and person["context_org"]:
        qs.append(f'"{name}" {person["context_org"]} Warwick')
    return qs


def run(conn, http, settings, limit: int | None = None) -> dict:
    search = Search(conn, http, settings)
    rows = conn.execute(
        "SELECT * FROM person WHERE screen_status IN ('HIT','MAYBE') AND fetch_status='pending' "
        + PROCESS_ORDER + " LIMIT ?",
        (limit if limit else -1,),
    ).fetchall()
    stats = {"people": 0, "pages_ok": 0, "pages_skipped": 0, "searches": 0}
    search_ok = search.available
    for p in rows:
        candidates = [(u, "wiki_ref") for u in json.loads(p["wiki_links"] or "[]")]
        if search_ok:
            for q in search_queries(p)[:1 if p["screen_status"] == "HIT" else 2]:
                try:
                    candidates += [(u, "search") for u, _ in search.urls(q)]
                    stats["searches"] += 1
                except SearchBudgetExhausted as exc:
                    log.warning("%s; continuing with Wikipedia references only", exc)
                    search_ok = False
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("search failed for %s: %s", p["name"], exc)
        for url, via in rank_urls(candidates, p["name"], settings.max_pages_per_person):
            existing = conn.execute("SELECT id, status, text_path FROM source WHERE url=?", (url,)).fetchone()
            if existing:
                if existing["status"] == "ok" and name_tokens_in_text(p["name"], read_text(existing["text_path"])):
                    link(conn, p["id"], existing["id"], via)
                continue
            res = pages.fetch(http, url, settings.user_agent)
            if res["status"] != "ok" or not res.get("text"):
                add_source(conn, settings, url=url, text=None, status=res["status"] if res["status"] != "ok" else "empty")
                stats["pages_skipped"] += 1
                continue
            # Text is kept even if this page is not about this person: it may be about another candidate.
            sid = add_source(conn, settings, url=url, text=res["text"], title=res["title"], date=res["date"])
            if name_tokens_in_text(p["name"], res["text"]):
                link(conn, p["id"], sid, via)
                stats["pages_ok"] += 1
            else:
                stats["pages_skipped"] += 1
        conn.execute("UPDATE person SET fetch_status='done', updated_at=? WHERE id=?", (now(), p["id"]))
        conn.commit()
        stats["people"] += 1
    return stats
