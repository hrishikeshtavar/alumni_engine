"""Sources and claims. Rule: no source, no claim; and a quote must exist in the stored source text."""

from __future__ import annotations

import json
import re
import unicodedata

from .db import now, today
from .pages import save_text
from .sources import TEMPLATE_SOURCE_TYPE, classify, domain_of


def norm_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―−]", "-", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def quote_in_text(quote: str, text: str) -> bool:
    q = norm_for_match(quote)
    return len(q) >= 8 and q in norm_for_match(text)


def add_source(conn, settings, *, url: str, text: str | None, title: str = "", date: str = "",
               status: str = "ok", tier_override: int | None = None) -> int:
    row = conn.execute("SELECT id FROM source WHERE url=?", (url,)).fetchone()
    if row:
        return row["id"]
    tier, kind, warwick_side = classify(url)
    if tier_override is not None:
        tier = tier_override
    text_path = save_text(settings.pages_dir, url, text) if text else None
    cur = conn.execute(
        "INSERT INTO source(url, domain, title, tier, source_type, warwick_side, published_at, accessed_at, "
        "text_path, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (url, domain_of(url), title, tier, TEMPLATE_SOURCE_TYPE.get(kind, "Other"), int(warwick_side),
         date or None, today(), text_path, status),
    )
    return cur.lastrowid


def link(conn, person_id: int, source_id: int, via: str) -> None:
    conn.execute("INSERT OR IGNORE INTO person_source(person_id, source_id, via) VALUES (?,?,?)",
                 (person_id, source_id, via))


def add_claim(conn, *, person_id: int, claim_type: str, data: dict, quote: str, source_id: int,
              origin: str, dept: str | None = None, verified: bool = True) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO claim(person_id, claim_type, data, dept, quote, source_id, verified, origin, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (person_id, claim_type, json.dumps(data, ensure_ascii=False), dept, (quote or "")[:600], source_id,
         int(verified), origin, now()),
    )


def read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""
