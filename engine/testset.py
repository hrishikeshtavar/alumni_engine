"""The fixed test set (testset/known_people.csv) and the whole-dataset checks. Runs on every export; any FAIL
stops the export. Add rows as WBS supplies known alumni and known non-alumni.

Expectations:
  CONFIRMED_SUBMIT             must be found, graded Confirmed, suggested Submit (known alumni)
  CONFIRMED                    must be found and graded Confirmed (known alumni whose role is not QS-type)
  NEVER_CONFIRMED_OR_PROBABLE  never Confirmed or Probable on any sheet (known non-alumni)
  NO_NAMESAKE_COUNTED          no counted quote names a different person with the same surname
  NO_COUNTED_QUOTE_ABOUT       no counted quote mentions the first name in 'detail' (shared bio pages)
  STAFF_NOT_IN_BROADER         not on Broader impact, and a staff flag (current or former) is set
  NOT_GRADED_ON_STAFF_QUOTES   if Confirmed/Probable, at least one counted study quote naming WBS; not on Broader
                               impact without an external impact marker
  DEPT_WARWICK_NOT_WBS         quotes containing 'detail' read 'Warwick not WBS'
WARN: the expectation's main point holds but a detail cannot be shown from evidence (for example a staff flag
with no accepted staff quote). Reported for a reviewer; does not block the export.
A person absent from this run's data passes vacuously and is reported as NOT IN DATA, except for the two
CONFIRMED expectations, which fail: a known alumnus who is not found is a recall failure.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from .attribution import name_occurrences, person_forms
from .stages import score as score_stage

DEFAULT_PATH = Path("testset/known_people.csv")
SHEET = {"P1_COUNT": "Candidate Register", "P1_FIX": "Candidate Register", "P2": "Candidate Register",
         "P3": "Candidate Register", "BROADER": "Broader impact", "LEAD": "Leads (weak evidence)",
         "STAFF": "WBS staff (excluded)", "HANDOFF": "Warwick not WBS", "DROP": "(not exported)"}


def load(path: Path | None = None) -> list[dict]:
    path = Path(path or DEFAULT_PATH)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh) if (r.get("name") or "").strip()]


def _find(conn, name: str):
    rows = conn.execute("SELECT * FROM person WHERE name=? AND extract_status='done'", (name,)).fetchall()
    if not rows:
        rows = conn.execute("SELECT * FROM person WHERE name LIKE ? AND extract_status='done'", (f"%{name}%",)).fetchall()
    return rows[0] if rows else None


def _counted(conn, pid):
    return conn.execute("SELECT c.quote, c.dept, c.origin FROM claim c WHERE c.person_id=? AND "
                        + score_stage.COUNTED_EDUCATION, (pid,)).fetchall()


def check_person(conn, t: dict) -> tuple[str, str]:
    exp, detail = t["expectation"], t.get("detail", "")
    p = _find(conn, t["name"])
    if p is None:
        if exp in ("CONFIRMED_SUBMIT", "CONFIRMED"):
            return "FAIL", "known alumnus not found in this run's data"
        return "NOT IN DATA", "not in this run's data (nothing to check)"
    sheet = SHEET.get(p["priority"], "(not scored)")
    where = f"{p['identity_grade']}, {p['submission_status']}, sheet: {sheet}"
    if exp == "CONFIRMED_SUBMIT":
        ok = p["identity_grade"] == "CONFIRMED" and p["submission_status"] == "Submit"
        return ("PASS" if ok else "FAIL"), where
    if exp == "CONFIRMED":
        return ("PASS" if p["identity_grade"] == "CONFIRMED" else "FAIL"), where
    if exp == "NEVER_CONFIRMED_OR_PROBABLE":
        return ("PASS" if p["identity_grade"] not in ("CONFIRMED", "PROBABLE") else "FAIL"), where
    if exp == "NO_NAMESAKE_COUNTED":
        forms = person_forms(p["name"], p["aliases"])
        leaks = [c["quote"] for c in _counted(conn, p["id"])
                 if c["origin"] != "wikidata" and "NAMESAKE" in name_occurrences(c["quote"] or "", forms)]
        n = conn.execute("SELECT COUNT(*) n FROM claim WHERE person_id=? AND rejected_reason='NAMESAKE'",
                         (p["id"],)).fetchone()["n"]
        return ("FAIL" if leaks else "PASS"), f"{n} quotes rejected as NAMESAKE; now {where}" + (
            f"; counted namesake quote: {leaks[0][:100]}" if leaks else "")
    if exp == "NO_COUNTED_QUOTE_ABOUT":
        leaks = [c["quote"] for c in _counted(conn, p["id"]) if detail.lower() in (c["quote"] or "").lower()]
        return ("FAIL" if leaks else "PASS"), where + (f"; counted quote mentions {detail}: {leaks[0][:100]}" if leaks else "")
    if exp == "STAFF_NOT_IN_BROADER":
        flags = f"Current WBS employee: {p['wbs_employee']}; Former WBS staff: {p['former_wbs_staff']}"
        has_flag = (p["wbs_employee"] or "No") != "No" or (p["former_wbs_staff"] or "No") != "No"
        if p["priority"] == "BROADER":
            return "FAIL", f"{where}; {flags}"
        if has_flag:
            return "PASS", f"{where}; {flags}"
        # Off Broader impact (the point of the test), but no accepted staff quote exists to set the flag from.
        # The flag is never set without evidence, so this is reported for a reviewer rather than blocking export.
        staff = conn.execute(
            "SELECT rejected_reason, COUNT(*) n FROM claim WHERE person_id=? AND claim_class='EMPLOYMENT' "
            "GROUP BY rejected_reason", (p["id"],)).fetchall()
        why = ", ".join(f"{r['n']} staff quote(s) rejected as {r['rejected_reason']}" for r in staff) \
            or "no staff quote found in the saved pages"
        return "WARN", f"{where}; staff flag not set: {why}"
    if exp == "NOT_GRADED_ON_STAFF_QUOTES":
        wbs_study = [c for c in _counted(conn, p["id"]) if c["dept"] == "WBS" and c["origin"] != "wikidata"]
        ok = (p["identity_grade"] not in ("CONFIRMED", "PROBABLE") or bool(wbs_study)) and \
             (p["priority"] != "BROADER" or bool(p["impact_marker"]))
        return ("PASS" if ok else "FAIL"), f"{where}; counted WBS study quotes: {len(wbs_study)}"
    if exp == "DEPT_WARWICK_NOT_WBS":
        q = conn.execute("SELECT dept FROM claim WHERE person_id=? AND quote LIKE ?",
                         (p["id"], f"%{detail}%")).fetchall()
        if not q:
            return "NOT IN DATA", f"no quote containing '{detail}'; {where}"
        ok = all(x["dept"] == "OTHER" for x in q)
        return ("PASS" if ok else "FAIL"), f"department check: {'Warwick not WBS' if ok else 'WBS or generic'}; {where}"
    return "FAIL", f"unknown expectation {exp}"


def run_all(conn, path: Path | None = None) -> list[dict]:
    """Every test in the set plus the two whole-dataset checks. Returns rows for the test report."""
    results = []
    for t in load(path):
        status, detail = check_person(conn, t)
        results.append({"test": f"{t['expectation']}", "name": t["name"], "group": t.get("group", ""),
                        "status": status, "detail": detail})
    bad7 = score_stage.integrity_errors(conn)
    results.append({"test": "7 No Confirmed/Probable row without an accepted study quote naming WBS", "name": "(all rows)",
                    "group": "automated", "status": "FAIL" if bad7 else "PASS",
                    "detail": "; ".join(bad7[:10]) or "every Confirmed/Probable row has one"})
    bad8 = score_stage.source_column_errors(conn)
    results.append({"test": "8 Every Source 1-3 URL has an accepted study quote", "name": "(all rows)",
                    "group": "automated", "status": "FAIL" if bad8 else "PASS",
                    "detail": "; ".join(bad8[:10]) or "all source URLs supported"})
    return results


def write_report(results: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"test_report_{time.strftime('%Y-%m-%d_%H%M')}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["status", "test", "name", "group", "detail"])
        w.writeheader()
        w.writerows(results)
    return out


def summary(results: list[dict]) -> str:
    lines = []
    for r in results:
        lines.append(f"  [{r['status']:11}] {r['name']:24} {r['test']}: {r['detail']}")
    fails = sum(r["status"] == "FAIL" for r in results)
    lines.append(f"  {len(results)} tests: {sum(r['status'] == 'PASS' for r in results)} pass, "
                 f"{sum(r['status'] == 'WARN' for r in results)} warn (reported, not blocking), "
                 f"{sum(r['status'] == 'NOT IN DATA' for r in results)} not in data, {fails} fail")
    return "\n".join(lines)
