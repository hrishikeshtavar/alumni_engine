"""REGRADE: re-check every saved quote with the current rules, re-score, and report what changed.

Uses only data already in the database (saved page text): no network, no API quota.
With --before <workbook>, the 'before' column is read from a previously exported workbook (for example the one
the stakeholder reviewed), so the comparison is against what people actually saw.
"""

from __future__ import annotations

import csv
import random
import time
import warnings
from collections import Counter
from pathlib import Path

import openpyxl

from .. import testset
from ..attribution import validate_claims
from ..db import now, today
from . import score as score_stage

SHEET = dict(testset.SHEET)
SHEET[None] = "(not scored)"
SHEET_ORDER = ["Candidate Register", "Broader impact", "Leads (weak evidence)", "WBS staff (excluded)",
               "Warwick not WBS", "(not exported)"]
STATUS_ORDER = ["Confirmed", "Probable", "Uncertain"]
TEMPLATE_STATUS = {"CONFIRMED": "Confirmed", "PROBABLE": "Probable"}


def _status(grade: str | None) -> str:
    return TEMPLATE_STATUS.get(grade or "", "Uncertain")


def _from_db(conn) -> dict[int, dict]:
    return {r["id"]: {"name": r["name"], "sheet": SHEET.get(r["priority"], "(not scored)"),
                      "status": _status(r["identity_grade"]), "grade": r["identity_grade"]}
            for r in conn.execute("SELECT id, name, identity_grade, priority FROM person WHERE extract_status='done'")}


def _from_workbook(conn, path: Path) -> dict[int, dict]:
    """Read sheet and identity status per person from an exported workbook, keyed by Engine person ID."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    by_name = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM person ORDER BY (qid IS NULL) DESC, id DESC")}
    ids = set(by_name.values()) | {r[0] for r in conn.execute("SELECT id FROM person")}
    out: dict[int, dict] = {}
    for ws in wb.worksheets:
        if ws.title not in SHEET_ORDER:
            continue
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None) or ()
        idx = {h: i for i, h in enumerate(header) if h}
        for r in rows:
            name = r[idx["Full name"]] if "Full name" in idx else None
            if not name:
                continue
            pid = r[idx["Engine person ID"]] if "Engine person ID" in idx else None
            pid = int(pid) if str(pid or "").isdigit() else None
            if pid not in ids:  # the record was merged or re-created since that export: match by name
                pid = by_name.get(name)
            if pid is None:
                continue
            out[pid] = {"name": name, "sheet": ws.title, "status": r[idx["WBS identity status"]] or "Uncertain",
                        "grade": None}
    return out


def _table(title: str, keys: list[str], before: Counter, after: Counter) -> list[str]:
    lines = [title, f"  {'':32}{'Before':>8}{'After':>8}{'Change':>8}"]
    for k in keys + sorted((set(before) | set(after)) - set(keys)):
        b, a = before.get(k, 0), after.get(k, 0)
        if b or a:
            lines.append(f"  {k:32}{b:>8}{a:>8}{a - b:>+8}")
    return lines


def _reason(conn, pid: int) -> str:
    rows = conn.execute(
        "SELECT rejected_reason, COUNT(*) n FROM claim WHERE person_id=? AND rejected_reason IS NOT NULL "
        "GROUP BY rejected_reason ORDER BY n DESC", (pid,)).fetchall()
    p = conn.execute("SELECT priority, impact_marker, wbs_employee, former_wbs_staff FROM person WHERE id=?",
                     (pid,)).fetchone()
    bits = [f"{r['rejected_reason']} x{r['n']}" for r in rows]
    if p and p["priority"] == "STAFF":
        bits.append("staff without an external impact marker")
    elif p and p["priority"] == "LEAD" and not p["impact_marker"]:
        bits.append("no external impact marker")
    return ", ".join(bits) or "rules re-applied"


def run(conn, settings, before_workbook: Path | None = None) -> dict:
    before = _from_workbook(conn, before_workbook) if before_workbook else _from_db(conn)
    stamp = now()
    conn.executemany(
        "INSERT OR REPLACE INTO grade_snapshot(person_id, name, identity_grade, priority, submission_status, taken_at) "
        "VALUES (?,?,?,?,?,?)", [(i, b["name"], b["status"], b["sheet"], None, stamp) for i, b in before.items()])
    conn.commit()
    from ..merge import auto_merge
    merged = auto_merge(conn)
    for keep, drop, _ in merged:  # compare the merged record against what its duplicate looked like before
        if drop in before and keep not in before:
            before[keep] = before[drop]
        before.pop(drop, None)
    reasons = validate_claims(conn, force=True)
    score_stage.run(conn, settings)
    after = _from_db(conn)
    if before_workbook:  # compare only what was exported before against where those people are now
        keys = set(before) | {i for i, a in after.items() if a["sheet"] not in ("(not exported)", "(not scored)")}
    else:
        keys = set(before) | set(after)

    lines = ["", "Before: " + (str(before_workbook) if before_workbook else "database state before this regrade"),
             "Quotes re-checked: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())), ""]
    lines += _table("Rows per sheet", SHEET_ORDER,
                    Counter(before[i]["sheet"] for i in keys if i in before),
                    Counter(after[i]["sheet"] for i in keys if i in after))
    lines.append("")
    lines += _table("WBS identity status", STATUS_ORDER,
                    Counter(before[i]["status"] for i in keys if i in before),
                    Counter(after[i]["status"] for i in keys if i in after))
    changes = []
    for i in sorted(keys, key=lambda k: (before.get(k) or after.get(k))["name"]):
        b = before.get(i) or {"name": after[i]["name"], "sheet": "(not exported)", "status": "-"}
        a = after.get(i) or {"sheet": "(merged or not scored)", "status": "-"}
        if (b["status"], b["sheet"]) != (a["status"], a["sheet"]):
            changes.append([b["name"], b["status"], a["status"], b["sheet"], a["sheet"], _reason(conn, i)])
    order = {s: n for n, s in enumerate(STATUS_ORDER)}
    changes.sort(key=lambda c: (order.get(c[1], 9), c[0]))
    lines += ["", f"Names whose grade or sheet changed: {len(changes)}"]
    for c in changes:
        lines.append(f"  {c[0]}: {c[1]} -> {c[2]}  ({c[3]} -> {c[4]})  {c[5]}")

    from .export import possible_duplicates
    dups = possible_duplicates(conn)
    lines += ["", f"Duplicate records merged (same name, at most one Wikidata ID): {len(merged)}"]
    lines += [f"  {name}: #{drop} merged into #{keep}" for keep, drop, name in merged]
    lines += ["", f"Possible duplicate records left for a human (same first and last name): {len(dups)}"]
    lines += [f"  {names}  (ids {ids}; merge with: python -m engine merge KEEP_ID DROP_ID)" for ids, names, _ in dups[:15]]
    results = testset.run_all(conn)
    report = testset.write_report(results, settings.out_dir)
    lines += ["", "Test set and checks", testset.summary(results), f"Test report written to {report}"]

    settings.out_dir.mkdir(parents=True, exist_ok=True)
    out = Path(settings.out_dir) / f"regrade_changes_{today()}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Name", "Status before", "Status after", "Sheet before", "Sheet after", "Reason"])
        w.writerows(changes)
    lines.append(f"Changes written to {out}")
    print("\n".join(lines))
    return {"changes": len(changes), "failed": sum(r["status"] == "FAIL" for r in results)}


def sample(conn, settings, n: int = 20, seed: int | None = None) -> Path:
    """A random sample of Confirmed and Probable rows with their accepted quotes, for checking by hand."""
    seed = seed if seed is not None else int(time.strftime("%Y%m%d"))
    people = conn.execute("SELECT * FROM person WHERE identity_grade IN ('CONFIRMED','PROBABLE') "
                          "AND priority NOT IN ('DROP') ORDER BY id").fetchall()
    picked = random.Random(seed).sample(list(people), min(n, len(people)))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Hand check"
    ws.append(["Engine person ID", "Full name", "Sheet", "WBS identity status", "Accepted quote 1", "URL 1",
               "Accepted quote 2", "URL 2", "Accepted quote 3", "URL 3", "External impact marker",
               "Your verdict (Correct / Wrong / Unsure)", "Notes"])
    for p in picked:
        quotes = conn.execute(
            "SELECT c.quote, s.url FROM claim c JOIN source s ON s.id=c.source_id WHERE c.person_id=? AND "
            + score_stage.COUNTED_EDUCATION + " ORDER BY CASE c.dept WHEN 'WBS' THEN 0 ELSE 1 END, s.tier LIMIT 3",
            (p["id"],)).fetchall()
        cells = []
        for q in list(quotes) + [None] * (3 - len(quotes)):
            cells += [q["quote"], q["url"]] if q else ["", ""]
        ws.append([p["id"], p["name"], SHEET.get(p["priority"]), _status(p["identity_grade"]), *cells,
                   p["impact_marker"] or "", "", ""])
    ws.append([])
    ws.append([f"Random seed {seed}: the same seed on the same data gives the same sample."])
    for col, width in zip("ABCDEFGHIJKLM", (10, 26, 22, 14, 60, 40, 60, 40, 60, 40, 50, 22, 30)):
        ws.column_dimensions[col].width = width
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    out = Path(settings.out_dir) / f"hand_check_sample_{today()}.xlsx"
    wb.save(out)
    return out


def explain(conn, pattern: str) -> str:
    """Everything behind one person's grade: records, flags, markers and every quote with its verdict."""
    rows = conn.execute("SELECT * FROM person WHERE name LIKE ? ORDER BY id", (f"%{pattern}%",)).fetchall()
    if not rows:
        return f"No person matching '{pattern}'."
    out = []
    for p in rows:
        out += ["", f"#{p['id']} {p['name']}  (Wikidata {p['qid'] or '-'})",
                f"  Sheet: {SHEET.get(p['priority'], '(not scored)')} | grade {p['identity_grade']} | "
                f"{p['submission_status']} | screen {p['screen_status']}",
                f"  Current WBS employee: {p['wbs_employee']} | Former WBS staff: {p['former_wbs_staff']}",
                f"  External impact marker: {p['impact_marker'] or '-'}",
                f"  Next action: {p['next_action'] or '-'}"]
        for m in conn.execute('SELECT m.universe_id, m.role_title, m.role_org, m.start, m."end" AS e FROM membership m '
                              "WHERE m.person_id=?", (p["id"],)):
            out.append(f"  List: {m['universe_id']}: {m['role_title']} {m['role_org']} {m['start'][:4]}-{(m['e'] or '')[:4]}")
        for c in conn.execute(
                "SELECT c.claim_type, c.claim_class, c.dept, c.rejected_reason, c.rejected_detail, c.quote, c.verified, "
                "c.origin, s.url FROM claim c LEFT JOIN source s ON s.id=c.source_id WHERE c.person_id=? "
                "ORDER BY c.rejected_reason IS NOT NULL, c.claim_type", (p["id"],)):
            verdict = "ACCEPTED" if not c["rejected_reason"] and c["verified"] else (
                c["rejected_reason"] or "QUOTE NOT ON PAGE")
            out.append(f"  [{verdict}] {c['claim_type']}/{c['claim_class']}/{c['dept'] or '-'} ({c['origin']}): "
                       f"\"{(c['quote'] or '')[:220]}\"  {c['url'] or ''}"
                       + (f"  [{c['rejected_detail']}]" if c["rejected_detail"] else ""))
    return "\n".join(out)
