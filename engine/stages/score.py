"""SCORE: deterministic rules over verified claims. No model decides a grade, a fit or a priority.

Outputs per person:
  identity_grade  CONFIRMED | PROBABLE | UNCERTAIN | WARWICK-NOT-WBS | NO_EVIDENCE
  qs_fit          ON-LIST | ELIGIBLE | BROADER
  attribution     0-3  (can an outsider tie them to Warwick from non-Warwick public text?)
  impact_level    1-5  (rule-proposed; reviewer confirms)
  priority        P1_COUNT | P1_FIX | P2 | P3 | BROADER | LEAD | HANDOFF | DROP
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..attribution import staff_flags, validate_claims
from ..impact import markers as impact_markers
from ..db import now, today
from .ingest import QS_FAMILIES

POLITICAL_TITLE = re.compile(r"\b(minister|prime minister|president|vice[- ]president|head of state|"
                             r"secretary of state|chancellor of the exchequer|premier)\b", re.I)
PRIORITY_ORDER = ["P1_COUNT", "P1_FIX", "P2", "P3", "BROADER", "LEAD", "STAFF", "HANDOFF", "DROP"]
FAMILY_CATEGORY = {
    "political": ("Public Impact and Profile", "Politics/ministerial"),
    "igo": ("Public Impact and Profile", "International organisation"),
    "central_bank": ("Public Impact and Profile", "Government/public service"),
    "university": ("Public Impact and Profile", "Academia/research"),
    "business": ("Senior Business Leadership", "Senior business leadership"),
}
NEXT_ACTION = {
    "P1_COUNT": "Spot-check the quoted sources and current role, then approve.",
    "P1_FIX": "Approve. The WBS link is not visible on non-Warwick sources: consider a WBS alumni story, or "
              "ask whether their official bio could mention WBS.",
    "P2": "Confirm the role is current and on a QS-type list.",
    "P3": "Confirm the department: evidence names Warwick but not WBS or a WBS programme.",
    "BROADER": "Broader impact track (outside QS list families): review the impact evidence.",
    "LEAD": "Weak evidence: check manually or drop.",
    "STAFF": "Current or former WBS/Warwick staff without both accepted study evidence and an external impact "
             "marker: excluded from Broader impact. Review only if they hold a senior role outside academia.",
    "HANDOFF": "Warwick graduate from another department: share with the central Warwick rankings team.",
    "DROP": "No verifiable Warwick evidence found.",
}


@dataclass
class Edu:
    dept: str
    tier: int
    domain: str
    warwick_side: bool
    known: bool = True  # False for unrecognised websites (source type "Other")


def year(s: str | None) -> int | None:
    m = re.match(r"(\d{4})", s or "")
    return int(m.group(1)) if m else None


def grade_identity(edu: list[Edu]) -> tuple[str, str, list[str]]:
    flags: list[str] = []
    wbs = [e for e in edu if e.dept == "WBS"]
    other = [e for e in edu if e.dept == "OTHER"]
    generic = [e for e in edu if e.dept == "GENERIC"]
    honorary = [e for e in edu if e.dept == "HONORARY"]
    strong_wbs_domains = {e.domain for e in wbs if e.tier <= 5}
    if wbs and any(e.tier <= 5 for e in other):
        flags.append("Also evidence of another Warwick department (may hold two Warwick degrees)")
    if honorary and not wbs:
        flags.append("Honorary Warwick degree found; honorary graduates are usually not counted as alumni")

    if any(e.warwick_side and e.tier <= 2 for e in wbs):
        return "CONFIRMED", "WBS or Warwick MBA named on an official Warwick/WBS page", flags
    # Two sources confirm only if at least one is a recognised type (government, university, register,
    # news...). Two unrecognised websites could be copies of the same spam article.
    if len(strong_wbs_domains) >= 2 and any(e.known and e.tier <= 5 for e in wbs):
        return "CONFIRMED", f"WBS or Warwick MBA named on {len(strong_wbs_domains)} independent sources", flags
    if len(strong_wbs_domains) >= 2:
        return "PROBABLE", "WBS named only on unrecognised websites; needs one recognised source", flags
    if len(strong_wbs_domains) == 1:
        return "PROBABLE", f"WBS or Warwick MBA named on one credible source ({next(iter(strong_wbs_domains))})", flags
    if wbs:
        return "UNCERTAIN", "WBS named only on Wikipedia/Wikidata/LinkedIn", flags
    if other:
        return "WARWICK-NOT-WBS", "Evidence points to another Warwick department", flags
    if generic:
        return "UNCERTAIN", "Warwick named without department or programme", flags
    if honorary:
        return "UNCERTAIN", "Honorary degree only", flags
    return "NO_EVIDENCE", "No verified Warwick education claim", flags


def qs_fit(members: list[dict], role_claims: list[dict], since: int) -> tuple[str, str, dict | None]:
    rank = {"ON-LIST": 2, "ELIGIBLE": 1}
    best, best_fit, best_family = None, "BROADER", ""
    for m in members:
        fam = m["family"]
        end_y = year(m["end"])
        in_window = end_y is None or end_y >= since
        fit = None
        if fam in QS_FAMILIES and in_window:
            fit = "ON-LIST" if (m["start"] or m["end"]) else "ELIGIBLE"
        elif fam == "university" and in_window:
            fit = "ELIGIBLE"
        if not fit:
            continue
        better = best is None or rank[fit] > rank[best_fit] or (
            rank[fit] == rank[best_fit] and (not m["end"]) and bool(best["end"]))
        if better:
            best, best_fit, best_family = m, fit, fam
    if best is None:
        for c in role_claims:  # e.g. a minister found via a manual list, role stated on a fetched page
            end_y = year(c.get("end"))
            if POLITICAL_TITLE.search(c.get("title") or "") and (end_y is None or end_y >= since):
                return "ELIGIBLE", "political", {"role_title": c.get("title"), "role_org": c.get("organisation") or "",
                                                  "start": c.get("start") or "", "end": c.get("end") or "",
                                                  "family": "political", "ingested_at": ""}
    return best_fit, best_family, best


def attribution_score(edu: list[Edu]) -> int:
    es = [e for e in edu if e.dept in ("WBS", "GENERIC")]
    if any(e.tier == 1 and not e.warwick_side for e in es):
        return 3
    if any(e.tier <= 5 and not e.warwick_side for e in es):
        return 2
    if any(e.warwick_side for e in es):
        return 1
    return 0


def impact_level(family: str, role: dict | None) -> int:
    title = (role or {}).get("role_title", "") or ""
    if family in ("political", "igo", "central_bank"):
        return 4 if "board member" in title.lower() else 5
    if family == "business":
        return 3 if "board member" in title.lower() else 4
    if family == "university":
        return 4
    return 3


def priority(grade: str, fit: str, attr: int) -> str:
    ok = grade in ("CONFIRMED", "PROBABLE")
    if grade == "WARWICK-NOT-WBS":
        return "HANDOFF"
    if grade == "NO_EVIDENCE":
        return "DROP"
    if ok and fit == "ON-LIST":
        return "P1_COUNT" if attr >= 2 else "P1_FIX"
    if ok and fit == "ELIGIBLE":
        return "P2"
    if grade == "UNCERTAIN" and fit in ("ON-LIST", "ELIGIBLE"):
        return "P3"
    if ok:
        return "BROADER"
    return "LEAD"


def submission_status(prio: str, grade: str) -> str:
    if prio == "P1_COUNT":
        return "Submit" if grade == "CONFIRMED" else "Consider"
    if prio in ("P1_FIX", "P2", "BROADER"):
        return "Consider"
    if prio in ("P3", "LEAD"):
        return "Hold"
    if prio == "STAFF":
        return "Do not submit"
    return "Do not submit"


def confidence(grade: str, flags: list[str], n_strong_sources: int) -> str:
    if flags:
        return "Needs verification"
    if grade == "CONFIRMED" and n_strong_sources >= 2:
        return "High"
    if grade in ("CONFIRMED", "PROBABLE"):
        return "Medium"
    return "Low"


def qs_visibility_score(fit: str, attr: int) -> int:
    if fit == "ON-LIST":
        return {3: 5, 2: 4}.get(attr, 3)
    if fit == "ELIGIBLE":
        return 3 if attr >= 2 else 2
    return 2 if attr >= 2 else 1


# Only these claims may support WBS identity: valid EDUCATION quotes about this person.
# DEPT_MISMATCH quotes are study at another Warwick department: they support 'Warwick not WBS' only.
COUNTED_EDUCATION = ("c.claim_type='education' AND c.verified=1 AND c.claim_class='EDUCATION' "
                     "AND (c.rejected_reason IS NULL OR c.rejected_reason='DEPT_MISMATCH') "
                     "AND c.dept IN ('WBS','OTHER','GENERIC')")
NAMESAKE_OR_STAFF_NOTE = "Previous evidence was a namesake, staff role or negated mention."
NO_MARKER_NOTE = ("WBS identity accepted, but no external impact marker found yet (academic titles do not count): "
                  "check for senior public, business or international roles, or a major national honour.")
STAFF_NOTE = "Works or worked at WBS; study at WBS not shown."


def run(conn, settings) -> dict:
    from ..merge import auto_merge
    auto_merge(conn)  # exact-name duplicates (at most one Wikidata ID) would otherwise be counted twice
    # Repair for records merged by v0.6, which kept the unfetched record's status: a record with extracted
    # quotes has been through extraction.
    conn.execute("UPDATE person SET screen_status=CASE WHEN screen_status IN ('HIT','MAYBE') THEN screen_status "
                 "ELSE 'MAYBE' END, fetch_status='done', extract_status='done' WHERE extract_status!='done' AND "
                 "EXISTS (SELECT 1 FROM claim c WHERE c.person_id=person.id AND c.origin IN ('rule','llm'))")
    conn.commit()
    validate_claims(conn)  # any claim not yet checked (databases from before the fix)
    since = settings.since_year
    rows = conn.execute("SELECT * FROM person WHERE extract_status='done'").fetchall()
    counts: dict[str, int] = {}
    for p in rows:
        edu_rows = conn.execute(
            "SELECT c.dept, s.tier, s.domain, s.warwick_side, s.source_type FROM claim c JOIN source s ON s.id=c.source_id "
            "WHERE c.person_id=? AND " + COUNTED_EDUCATION, (p["id"],)).fetchall()
        edu = [Edu(r["dept"], r["tier"] or 7, r["domain"] or "", bool(r["warwick_side"]),
                   (r["source_type"] or "Other") != "Other") for r in edu_rows]
        grade, note, flags = grade_identity(edu)
        rejected = {r["rejected_reason"]: r["n"] for r in conn.execute(
            "SELECT rejected_reason, COUNT(*) n FROM claim WHERE person_id=? AND claim_type='education' AND verified=1 "
            "AND rejected_reason IS NOT NULL GROUP BY rejected_reason", (p["id"],))}
        if rejected.get("HONORARY") and grade not in ("CONFIRMED", "PROBABLE"):
            flags.append("Honorary Warwick degree or position found; honorary titles are not counted as study")
        employee, former_staff = staff_flags(conn, p["id"])

        members = [dict(r) for r in conn.execute(
            'SELECT m.role_title, m.role_org, m.start, m."end" AS "end", u.family, u.ingested_at '
            "FROM membership m JOIN universe u ON u.id=m.universe_id WHERE m.person_id=? AND m.role_title!='Educated at'",
            (p["id"],))]
        role_claims = [json.loads(r["data"]) | {"accessed": r["accessed_at"]} for r in conn.execute(
            "SELECT c.data, s.accessed_at FROM claim c JOIN source s ON s.id=c.source_id "
            "WHERE c.person_id=? AND c.claim_type='role' AND c.verified=1 AND c.rejected_reason IS NULL", (p["id"],))]
        fit, family, best = qs_fit(members, role_claims, since)
        attr = attribution_score(edu)
        impact = impact_level(family, best)
        prio = priority(grade, fit, attr)
        lost_evidence = any(rejected.get(k) for k in ("NAMESAKE", "UNSUPPORTED_SOURCE", "EMPLOYMENT", "NEGATED",
                                                       "HONORARY"))
        if grade == "NO_EVIDENCE" and (lost_evidence or rejected):
            prio = "LEAD"  # keep them visible for a reviewer rather than dropping them silently
        is_staff = employee != "No" or former_staff != "No"
        marker_list = impact_markers(conn, p) if prio == "BROADER" or is_staff else []
        no_marker = False
        if prio == "BROADER":
            # Broader impact needs accepted study evidence AND an external impact marker; staff never get in on
            # academic titles alone.
            if is_staff and not marker_list:
                prio = "STAFF"
            elif not marker_list:
                prio, no_marker = "LEAD", True
        status = p["decision"] or submission_status(prio, grade)
        n_strong = conn.execute(
            "SELECT COUNT(DISTINCT s.domain) n FROM person_source ps JOIN source s ON s.id=ps.source_id "
            "WHERE ps.person_id=? AND s.tier<=5 AND s.status='ok'", (p["id"],)).fetchone()["n"]
        conf = confidence(grade, flags, n_strong)

        # Current role: an open-ended membership first, then a current role stated on a fetched page.
        current = next((m for m in members if not m["end"] and m["family"] in QS_FAMILIES), None) \
            or next((m for m in members if not m["end"]), None)
        role_as_of, role_note = "", ""
        if current:
            cur_role, cur_org = current["role_title"], current["role_org"]
            role_as_of = (current.get("ingested_at") or "")[:10]
            role_note = " Role and dates come from Wikidata: check it is current on an official page."
        else:
            rc = next((c for c in role_claims if c.get("is_current")), None)
            if rc:
                cur_role, cur_org, role_as_of = rc.get("title") or "", rc.get("organisation") or "", rc.get("accessed", "")
            elif best:
                cur_role = f"Former: {best['role_title']} (until {best['end'][:4] or 'unknown'})"
                cur_org, role_as_of = best["role_org"], (best.get("ingested_at") or "")[:10]
            else:
                cur_role, cur_org = "", p["context_org"] or ""
        if employee == "No" and any("warwick" in (c.get("organisation") or "").lower() and c.get("end") in (None, "")
                                    for c in role_claims):
            employee = "Yes (Warwick, department not stated)"
        stream, category = FAMILY_CATEGORY.get(family, ("", ""))
        action = NEXT_ACTION[prio] + (role_note if prio not in ("DROP", "HANDOFF") else "")
        if grade == "NO_EVIDENCE" and lost_evidence:
            action = NAMESAKE_OR_STAFF_NOTE + " " + action
        if no_marker:
            action = NO_MARKER_NOTE + " " + action
        if (employee != "No" or former_staff != "No") and grade not in ("CONFIRMED", "PROBABLE"):
            action += " " + STAFF_NOTE
        if flags:
            action += " " + "; ".join(flags) + "."
        wbs_employee = employee
        conn.execute(
            "UPDATE person SET identity_grade=?, identity_note=?, qs_fit=?, qs_family=?, attribution=?, "
            "impact_level=?, stream=?, category=?, priority=?, submission_status=?, confidence=?, next_action=?, "
            "current_role=?, current_org=?, role_as_of=?, wbs_employee=?, former_wbs_staff=?, impact_marker=?, "
            "updated_at=? WHERE id=?",
            (grade, note + (" | " + "; ".join(flags) if flags else ""), fit, family, attr, impact, stream, category,
             prio, status, conf, action.strip(), cur_role, cur_org, role_as_of or today(), wbs_employee, former_staff,
             " | ".join(marker_list), now(), p["id"]),
        )
        counts[prio] = counts.get(prio, 0) + 1
    conn.commit()
    return counts


def integrity_errors(conn) -> list[str]:
    """Every Confirmed or Probable person must rest on at least one valid EDUCATION quote naming WBS
    from a credible source. Returns the names that do not (an empty list means the check passes)."""
    bad = conn.execute(
        "SELECT p.name, p.identity_grade FROM person p WHERE p.identity_grade IN ('CONFIRMED','PROBABLE') "
        "AND NOT EXISTS (SELECT 1 FROM claim c JOIN source s ON s.id=c.source_id WHERE c.person_id=p.id "
        "AND c.claim_type='education' AND c.verified=1 AND c.claim_class='EDUCATION' AND c.rejected_reason IS NULL "
        "AND c.dept='WBS' AND (s.tier<=5 OR s.warwick_side=1))").fetchall()
    return [f"{r['name']} ({r['identity_grade']})" for r in bad]


def identity_sources(conn, person_id: int, limit: int = 3) -> list[dict]:
    """The pages allowed in Source 1 to 3: only pages with at least one accepted EDUCATION quote for this person
    (WBS quotes first, then by source tier). Pages without one stay in the Evidence sheet as context."""
    return [dict(r) for r in conn.execute(
        "SELECT s.*, MIN(CASE c.dept WHEN 'WBS' THEN 0 WHEN 'GENERIC' THEN 1 ELSE 2 END) AS rank FROM claim c "
        "JOIN source s ON s.id=c.source_id WHERE c.person_id=? AND " + COUNTED_EDUCATION.replace("c.", "c.") +
        " GROUP BY s.id ORDER BY rank, s.tier LIMIT ?", (person_id, limit))]


def source_column_errors(conn) -> list[str]:
    """Test 8: every URL that will appear in Source 1 to 3 has an accepted EDUCATION quote for that person."""
    errors = []
    for p in conn.execute("SELECT id, name FROM person WHERE priority IS NOT NULL AND priority!='DROP'"):
        for src in identity_sources(conn, p["id"]):
            ok = conn.execute("SELECT 1 FROM claim c WHERE c.person_id=? AND c.source_id=? AND " + COUNTED_EDUCATION,
                              (p["id"], src["id"])).fetchone()
            if not ok:
                errors.append(f"{p['name']}: {src['url']}")
    return errors
