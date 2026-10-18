"""INGEST: load finite universes of people into the database.

Impact-first universes (QS-type lists): political, igo, central_bank, business, university.
Alumni-first universes: the Wikidata seed of Warwick-educated people, manual CSV lists, and the
previous NebulaOne output.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import openpyxl

from .. import wikidata as wd
from ..db import get_meta, now, set_meta
from ..names import match_band, normalize_name

log = logging.getLogger(__name__)

# QS publishes four list families. 'university' is kept separate: QS names higher education
# in its aims, but university leaders are not one of the four published families.
QS_FAMILIES = {"political", "igo", "central_bank", "business"}
FAMILY_LABEL = {
    "political": "Political leaders (cabinet, head of state/government)",
    "igo": "Organisations (intergovernmental bodies)",
    "central_bank": "Organisations (central banks)",
    "business": "Business leaders (listed companies)",
    "university": "Higher education leaders",
    "alumni_first": "Alumni-first source",
    "manual": "Manual list",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def upsert_person(conn, *, qid: str | None, name: str, country: str = "", context_org: str = "") -> int:
    ts = now()
    if qid:
        row = conn.execute("SELECT id FROM person WHERE qid=?", (qid,)).fetchone()
        if row:
            return row["id"]
        if not name or name == qid:
            name = qid
        cur = conn.execute(
            "INSERT INTO person(qid, name, norm_name, country, context_org, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (qid, name, normalize_name(name), country, context_org, ts, ts),
        )
        return cur.lastrowid
    # No shared ID: merge only on a strong match (same name AND same organisation).
    norm = normalize_name(name)
    for row in conn.execute("SELECT id, name, context_org FROM person WHERE norm_name=?", (norm,)):
        if match_band(name, context_org, row["name"], row["context_org"] or "") == "strong":
            return row["id"]
    cur = conn.execute(
        "INSERT INTO person(qid, name, norm_name, country, context_org, created_at, updated_at) "
        "VALUES (NULL,?,?,?,?,?,?)",
        (name, norm, country, context_org, ts, ts),
    )
    return cur.lastrowid


def add_membership(conn, person_id: int, universe_id: str, role: str = "", org: str = "",
                   start: str = "", end: str = "") -> None:
    conn.execute(
        'INSERT OR IGNORE INTO membership(person_id, universe_id, role_title, role_org, start, "end") '
        "VALUES (?,?,?,?,?,?)",
        (person_id, universe_id, role or "", org or "", (start or "")[:10], (end or "")[:10]),
    )


def start_universe(conn, uid: str, family: str, label: str, country: str = "", refresh: bool = False) -> bool:
    row = conn.execute("SELECT status FROM universe WHERE id=?", (uid,)).fetchone()
    if row and row["status"] == "done" and not refresh:
        log.info("universe %s already ingested (use --refresh to redo)", uid)
        return False
    conn.execute(
        "INSERT INTO universe(id, family, label, country, status) VALUES (?,?,?,?, 'pending') "
        "ON CONFLICT(id) DO UPDATE SET status='pending', error=NULL",
        (uid, family, label, country),
    )
    conn.commit()
    return True


def finish_universe(conn, uid: str, count: int, error: str | None = None) -> None:
    conn.execute(
        "UPDATE universe SET status=?, member_count=?, error=?, ingested_at=? WHERE id=?",
        ("error" if error else "done", count, error, now(), uid),
    )
    conn.commit()


def _clean_label(label: str, qid: str) -> str:
    # The label service returns the QID when no label exists in the requested languages.
    return "" if not label or label == qid else label


# ---------------------------------------------------------------------------
# Warwick entities and countries
# ---------------------------------------------------------------------------
def warwick_entities(conn, http) -> dict:
    cached = get_meta(conn, "warwick")
    if cached:
        return cached

    def exact(text: str) -> str | None:
        for hit in wd.search_items(http, text):
            if hit["label"].strip().lower() == text.lower():
                return hit["id"]
        return None

    warwick_qid = exact("University of Warwick")
    wbs_qid = exact("Warwick Business School")
    if not warwick_qid or not wbs_qid:
        raise RuntimeError("Could not find 'University of Warwick' and 'Warwick Business School' in Wikidata.")
    parts = {warwick_qid: "University of Warwick", wbs_qid: "Warwick Business School"}
    try:
        for r in wd.sparql(http, wd.warwick_parts_query(warwick_qid)):
            parts.setdefault(r["org"], r.get("orgLabel") or r["org"])
    except Exception as exc:  # noqa: BLE001
        # Not fatal: without department items the screen still works from WBS + Warwick + page text.
        log.warning("could not list Warwick departments (%s); continuing with WBS and Warwick only", exc)
    info = {"warwick": warwick_qid, "wbs": wbs_qid, "parts": parts}
    set_meta(conn, "warwick", info)
    log.info("Warwick=%s WBS=%s, %d Warwick units", warwick_qid, wbs_qid, len(parts))
    return info


def countries(conn, http) -> dict[str, str]:
    return {iso: v["label"] for iso, v in country_index(conn, http).items()}


def country_index(conn, http) -> dict[str, dict]:
    """{ISO: {'qid', 'label'}} for current sovereign states (cached)."""
    cached = get_meta(conn, "country_index")
    if cached:
        return cached
    rows = wd.sparql(http, wd.countries_query())
    result = {r["iso"].upper(): {"qid": r["c"], "label": r.get("cLabel", r["iso"])} for r in rows if r.get("iso")}
    set_meta(conn, "country_index", result)
    return result


# ---------------------------------------------------------------------------
# universes
# ---------------------------------------------------------------------------
def ingest_seed(conn, http, refresh: bool = False) -> int:
    uid = "alumni:wikidata_seed"
    if not start_universe(conn, uid, "alumni_first", "Wikidata: educated at Warwick or a Warwick unit", "", refresh):
        return 0
    info = warwick_entities(conn, http)
    try:
        rows = wd.sparql(http, wd.seed_query(list(info["parts"].keys())))
    except Exception as exc:  # noqa: BLE001
        finish_universe(conn, uid, 0, str(exc)[:500])
        raise
    seen = set()
    for r in rows:
        qid = r["person"]
        pid = upsert_person(conn, qid=qid, name=_clean_label(r.get("personLabel", ""), qid))
        add_membership(conn, pid, uid, "Educated at", r.get("instLabel", ""), "", r.get("end", ""))
        seen.add(pid)
    finish_universe(conn, uid, len(seen))
    return len(seen)


def _ingest_rows(conn, uid: str, rows: list[dict], country: str) -> int:
    seen = set()
    for r in rows:
        qid = r.get("person")
        if not qid:
            continue
        pid = upsert_person(conn, qid=qid, name=_clean_label(r.get("personLabel", ""), qid), country=country)
        role = _clean_label(r.get("posLabel", ""), r.get("pos", "")) or r.get("role", "")
        org = _clean_label(r.get("orgLabel", ""), r.get("org", ""))
        add_membership(conn, pid, uid, role, org, r.get("start", ""), r.get("end", ""))
        seen.add(pid)
    return len(seen)


def _holders_in_batches(conn, http, uid: str, qids: list[str], query_fn, country: str) -> tuple[int, str | None]:
    """Run a holders query over qids in batches; returns (people, first error)."""
    seen, error = 0, None
    for i in range(0, len(qids), wd.BATCH):
        batch = qids[i:i + wd.BATCH]
        try:
            rows = wd.sparql(http, query_fn(batch))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: batch %d failed (%s); continuing", uid, i // wd.BATCH + 1, str(exc)[:120])
            error = error or str(exc)[:300]
            continue
        seen += _ingest_rows(conn, uid, rows, country)
        conn.commit()
    return seen, error


def political_positions(conn, http) -> dict[str, list[str]]:
    """{country QID: [office QIDs]} for national ministerial / head of state / head of government offices.

    Wikidata records an office in two ways: as an instance of 'minister' (P31), or as a sub-class of it
    (P279, e.g. 'Prime Minister of the United Kingdom'). Both are collected. Built once and cached."""
    cached = get_meta(conn, "political_positions_v2")
    if cached:
        return cached
    classes = [r["cls"] for r in wd.sparql(http, wd.political_classes_query()) if r.get("cls")]
    log.info("political: %d office classes; listing offices (one-time, cached afterwards)", len(classes))
    by_pos: dict[str, dict] = {}

    def add(rows):
        for r in rows:
            entry = by_pos.setdefault(r["pos"], {"j": set(), "k": set()})
            if r.get("j"):
                entry["j"].add(r["j"])
            if r.get("k"):
                entry["k"].add(r["k"])

    # 1. offices that are instances of these classes
    for i in range(0, len(classes), 200):
        add(wd.sparql(http, wd.positions_of_classes_query(classes[i:i + 200])))
    log.info("political: instances done; now offices recorded as sub-classes")
    # 2. the classes themselves: specific offices are often modelled as sub-classes
    for i in range(0, len(classes), 400):
        add(wd.sparql(http, wd.jurisdictions_query(classes[i:i + 400])))
        if (i // 400) % 25 == 0:
            log.info("political: checked %d/%d sub-classes", min(i + 400, len(classes)), len(classes))
    result: dict[str, list[str]] = {}
    for pos, e in by_pos.items():
        # National offices: jurisdiction is the country itself; or, with no jurisdiction recorded, the country.
        # Offices whose jurisdiction is a state or province are left out.
        targets = e["j"] if e["j"] else e["k"]
        for c in targets:
            result.setdefault(c, []).append(pos)
    set_meta(conn, "political_positions_v2", result)
    log.info("political: %d offices across %d jurisdictions", len(by_pos), len(result))
    return result


def ingest_family_by_country(conn, http, family: str, isos: list[str], since: int, refresh: bool = False) -> int:
    if family not in ("political", "business", "university"):
        raise ValueError(f"unknown per-country family {family}")
    index = country_index(conn, http)
    positions = political_positions(conn, http) if family == "political" else {}
    total = 0
    for iso in isos:
        iso = iso.upper()
        info = index.get(iso)
        if not info:
            log.warning("unknown country code %s; skipped", iso)
            continue
        uid = f"{family}:{iso}"
        if not start_universe(conn, uid, family, f"{FAMILY_LABEL[family]}: {info['label']}", iso, refresh):
            continue
        try:
            if family == "political":
                qids = positions.get(info["qid"], [])
                n, err = _holders_in_batches(conn, http, uid, qids, lambda b: wd.holders_query(b, since), info["label"])
                found = f"{len(qids)} offices"
            else:
                listq = wd.listed_companies_query if family == "business" else wd.universities_query
                qids = [r["org"] for r in wd.sparql(http, listq(iso)) if r.get("org")]
                n, err = _holders_in_batches(
                    conn, http, uid, qids,
                    lambda b: wd.org_holders_query(b, since, with_board=(family == "business")), info["label"])
                found = f"{len(qids)} organisations"
        except Exception as exc:  # noqa: BLE001
            log.warning("universe %s failed: %s", uid, str(exc)[:200])
            finish_universe(conn, uid, 0, str(exc)[:500])
            continue
        finish_universe(conn, uid, n, err if n == 0 and err else None)
        log.info("%s: %s, %d people", uid, found, n)
        total += n
    return total


def ingest_global(conn, http, family: str, since: int, refresh: bool = False) -> int:
    class_key = {"igo": "igo", "central_bank": "central_bank"}[family]
    uid = f"{family}:global"
    if not start_universe(conn, uid, family, FAMILY_LABEL[family], "", refresh):
        return 0
    try:
        qids = [r["org"] for r in wd.sparql(http, wd.orgs_of_class_query(class_key)) if r.get("org")]
    except Exception as exc:  # noqa: BLE001
        finish_universe(conn, uid, 0, str(exc)[:500])
        raise
    n, err = _holders_in_batches(conn, http, uid, qids, lambda b: wd.org_holders_query(b, since), "")
    finish_universe(conn, uid, n, err if n == 0 and err else None)
    log.info("%s: %d organisations, %d people", uid, len(qids), n)
    return n


def ingest_csv(conn, path: Path, universe: str, family: str = "manual", refresh: bool = False) -> int:
    """CSV columns (header row): name, role, organisation, country, start, end, source_url (only name required).

    Use this for official lists you copy by hand: WBS alumni stories, honorary graduates, award rolls,
    public appointment lists, or names-only uploads from staff with CRM access.
    """
    uid = f"{family}:{universe}"
    if not start_universe(conn, uid, family, f"CSV: {universe}", "", refresh):
        return 0
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name = row.get("name") or row.get("full name")
            if not name:
                continue
            org = row.get("organisation") or row.get("organization") or ""
            pid = upsert_person(conn, qid=row.get("wikidata_id") or None, name=name,
                                country=row.get("country", ""), context_org=org)
            add_membership(conn, pid, uid, row.get("role", ""), org, row.get("start", ""), row.get("end", ""))
            if row.get("source_url"):
                conn.execute("UPDATE person SET wiki_links=? WHERE id=? AND wiki_links='[]'",
                             (json.dumps([row["source_url"]]), pid))
            n += 1
    finish_universe(conn, uid, n)
    return n


def ingest_previous(conn, path: Path, refresh: bool = False) -> int:
    """Load the previous NebulaOne output (e.g. '2026-08-19 QS Alumni Impact Data - academic.xlsx')."""
    uid = "alumni_first:previous_nebula"
    if not start_universe(conn, uid, "alumni_first", "Previous NebulaOne run (re-verified here)", "", refresh):
        return 0
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    n = 0
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            continue
        idx = {str(h).strip().lower(): i for i, h in enumerate(header) if h}
        name_col = idx.get("full name")
        if name_col is None:
            continue
        org_col = idx.get("current organisation", idx.get("current organization"))
        role_col = idx.get("current role")
        country_col = idx.get("country / region")
        for r in rows:
            raw = r[name_col] if name_col < len(r) else None
            if not raw:
                continue
            # remove footnote debris such as "Nigel M. Healey 1"
            name = str(raw).replace("\xa0", " ").strip()
            name = " ".join(t for t in name.split() if not t.isdigit())
            org = str(r[org_col]).strip() if org_col is not None and r[org_col] else ""
            role = str(r[role_col]).strip() if role_col is not None and r[role_col] else ""
            country = str(r[country_col]).split(";")[0].strip() if country_col is not None and r[country_col] else ""
            pid = upsert_person(conn, qid=None, name=name, country=country, context_org=org)
            add_membership(conn, pid, uid, role, org)
            n += 1
    finish_universe(conn, uid, n)
    return n
