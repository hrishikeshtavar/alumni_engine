"""Merging duplicate person records.

Two records for one person (one from Wikidata, one from the previous NebulaOne list, say) would be counted twice.
auto_merge() joins records with exactly the same normalised name when at most one of them has a Wikidata ID, so
they cannot be two different Wikidata people. Near matches ('Walter Baets' / 'Walter R. J. Baets') are only
flagged, and merged by hand with `python -m engine merge KEEP_ID DROP_ID`.
"""

from __future__ import annotations

import json

from .db import now


def merge(conn, keep: int, drop: int) -> None:
    if keep == drop:
        raise ValueError("Cannot merge a record into itself.")
    k = conn.execute("SELECT * FROM person WHERE id=?", (keep,)).fetchone()
    d = conn.execute("SELECT * FROM person WHERE id=?", (drop,)).fetchone()
    if not k or not d:
        raise ValueError(f"No person with id {keep if not k else drop}.")
    if k["qid"] and d["qid"] and k["qid"] != d["qid"]:
        raise ValueError(f"{k['name']} ({k['qid']}) and {d['name']} ({d['qid']}) are different Wikidata people.")
    aliases = set()
    for row in (k, d):
        try:
            aliases |= {a for a in json.loads(row["aliases"] or "[]") if isinstance(a, str)}
        except (ValueError, TypeError):
            pass
    if d["name"] != k["name"]:
        aliases.add(d["name"])
    for table, cols in (("membership", "universe_id, role_title, role_org, start"), ("person_source", "source_id")):
        conn.execute(f"UPDATE OR IGNORE {table} SET person_id=? WHERE person_id=?", (keep, drop))
        conn.execute(f"DELETE FROM {table} WHERE person_id=?", (drop,))
    conn.execute("UPDATE OR IGNORE claim SET person_id=? WHERE person_id=?", (keep, drop))
    conn.execute("DELETE FROM claim WHERE person_id=?", (drop,))
    conn.execute("DELETE FROM grade_snapshot WHERE person_id=?", (drop,))
    fill = {c: d[c] for c in ("wiki_title", "wiki_lang", "description", "country", "current_role", "current_org",
                              "decision", "decision_note") if not k[c] and d[c]}
    qid = k["qid"] or d["qid"]
    # Keep the furthest pipeline progress of the two: if only the dropped record had been fetched and
    # extracted, the merged one must still be scored.
    order = {"pending": 0, "MISS": 1, "MAYBE": 2, "HIT": 3, "done": 4}
    for col in ("screen_status", "fetch_status", "extract_status"):
        if order.get(d[col] or "pending", 0) > order.get(k[col] or "pending", 0):
            fill[col] = d[col]
    conn.execute("DELETE FROM person WHERE id=?", (drop,))  # first, so the qid is free to move
    sets = ", ".join(f"{c}=?" for c in fill)
    conn.execute(f"UPDATE person SET aliases=?, qid=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
                 (json.dumps(sorted(aliases)), qid, now(), *fill.values(), keep))
    # The merged record needs its quotes re-checked and re-scored.
    conn.execute("UPDATE claim SET claim_class=NULL WHERE person_id=?", (keep,))
    conn.commit()


def auto_merge(conn) -> list[tuple[int, int, str]]:
    """Merge records with the same normalised name when at most one has a Wikidata ID. Returns (kept, dropped, name)."""
    groups: dict[str, list] = {}
    for p in conn.execute("SELECT id, name, norm_name, qid FROM person WHERE norm_name IS NOT NULL AND norm_name!='' "
                          "ORDER BY (qid IS NULL), id"):
        groups.setdefault(p["norm_name"], []).append(p)
    done = []
    for rows in groups.values():
        if len(rows) < 2 or sum(1 for r in rows if r["qid"]) > 1:
            continue
        keep = rows[0]
        for r in rows[1:]:
            merge(conn, keep["id"], r["id"])
            done.append((keep["id"], r["id"], keep["name"]))
    return done
