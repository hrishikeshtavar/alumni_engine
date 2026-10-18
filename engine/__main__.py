"""Command line: python -m engine <command> [options]. Every command is safe to stop and re-run."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Settings
from .db import connect, log_run, now
from .http import Http

log = logging.getLogger("engine")
DEFAULT_TEMPLATE = Path("templates/Alumni candidate output template.xlsx")

# A practical starting set: countries where WBS alumni are likely to hold public office or lead
# listed companies. This is a guess to be replaced by measured yields; use --all for every country.
STARTER_COUNTRIES = ["GB", "NG", "GH", "KE", "ZA", "UG", "TZ", "RW", "ZW", "ZM", "BW", "MW", "SL", "IN", "PK",
                     "BD", "LK", "NP", "MY", "SG", "TH", "ID", "PH", "VN", "CN", "HK", "TW", "JP", "KR",
                     "AE", "SA", "QA", "KW", "BH", "OM", "JO", "EG", "TR", "CY", "GR", "DE", "FR", "NL", "IT",
                     "ES", "IE", "US", "CA", "BR", "MX", "AU", "NZ"]


def _ctx(require_contact: bool = True):
    s = Settings.from_env()
    s.ensure_dirs()
    if require_contact and not s.contact_email:
        sys.exit("Set CONTACT_EMAIL in .env first (Wikimedia requires a contact address from automated clients).")
    conn = connect(s.db_path)
    return s, conn, Http(conn, s.user_agent)


def cmd_doctor(args):
    from . import wikidata as wd
    from .llm import LLM
    from .search import Search
    from .stages.ingest import warwick_entities
    s, conn, http = _ctx()
    ok = True
    print(f"Database: {s.db_path}")
    labels = wd.labels_for(http, list(wd.EXPECTED_LABEL))
    for qid, expected in wd.EXPECTED_LABEL.items():
        got = labels.get(qid, "")
        flag = "ok" if expected.lower() in got.lower() else "MISMATCH"
        ok &= flag == "ok"
        print(f"  {qid:10} expected '{expected}', Wikidata says '{got}' [{flag}]")
    try:
        info = warwick_entities(conn, http)
        print(f"  University of Warwick = {info['warwick']}, Warwick Business School = {info['wbs']}, "
              f"{len(info['parts'])} Warwick units known")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  Could not resolve Warwick/WBS in Wikidata: {str(exc)[:200]}")
    llm = LLM(conn, http, s)
    print(f"LLM: {llm.describe()}")
    if llm.available and args.llm:
        print("  test call:", llm.json('Return JSON {"ok": true}.'))
    srch = Search(conn, http, s)
    print(f"Search: {s.search_provider if srch.available else 'not configured (Wikipedia references only)'}"
          + (f", {srch.used_this_month()}/{s.search_monthly_cap} used this month" if srch.available else ""))
    print("All checks passed." if ok else "Fix the mismatched Wikidata IDs in engine/wikidata.py before ingesting.")


def cmd_ingest(args):
    from .stages import ingest
    s, conn, http = _ctx()
    started, n = now(), 0
    if args.family == "seed":
        n = ingest.ingest_seed(conn, http, args.refresh)
    elif args.family in ("igo", "central_bank"):
        n = ingest.ingest_global(conn, http, args.family, s.since_year, args.refresh)
    else:
        if args.all:
            isos = sorted(ingest.countries(conn, http))
        elif args.countries:
            isos = [c.strip() for c in args.countries.split(",") if c.strip()]
        else:
            isos = STARTER_COUNTRIES
        n = ingest.ingest_family_by_country(conn, http, args.family, isos, s.since_year, args.refresh)
    log_run(conn, f"ingest {args.family}", started, n)
    print(f"Ingested {n} people.")


def cmd_ingest_csv(args):
    from .stages import ingest
    s, conn, _ = _ctx()
    n = ingest.ingest_csv(conn, Path(args.file), args.universe, args.family, args.refresh)
    print(f"Ingested {n} rows from {args.file}.")


def cmd_ingest_previous(args):
    from .stages import ingest
    s, conn, _ = _ctx()
    n = ingest.ingest_previous(conn, Path(args.file), args.refresh)
    print(f"Ingested {n} people from the previous run.")


def cmd_screen(args):
    from .stages import screen
    s, conn, http = _ctx()
    started = now()
    counts = screen.run(conn, http, s, args.limit)
    log_run(conn, "screen", started, sum(counts.values()), str(counts))
    print(f"Screened: {counts}")


def cmd_fetch(args):
    from .stages import fetch
    s, conn, http = _ctx()
    started = now()
    stats = fetch.run(conn, http, s, args.limit)
    log_run(conn, "fetch", started, stats["people"], str(stats))
    print(f"Fetch: {stats}")


def cmd_extract(args):
    from .stages import extract
    s, conn, http = _ctx()
    started = now()
    stats = extract.run(conn, http, s, args.limit)
    log_run(conn, "extract", started, stats["people"],
            f"model={stats['model']}; new model calls={stats['model_calls']}; {stats}")
    print(f"Extract: {stats}")


def cmd_score(args):
    from .stages import score
    s, conn, _ = _ctx(require_contact=False)
    counts = score.run(conn, s)
    print(f"Scored: {counts}")


def cmd_export(args):
    from .stages import export, score
    s, conn, _ = _ctx(require_contact=False)
    score.run(conn, s)
    template = Path(args.template)
    if not template.exists():
        sys.exit(f"Template not found: {template}. Put 'Alumni candidate output template.xlsx' in templates/ "
                 "or pass --template.")
    try:
        out = export.run(conn, s, template, Path(args.out) if args.out else None)
    except export.IntegrityError as exc:
        sys.exit(str(exc))
    print(f"Wrote {out}")


def cmd_regrade(args):
    from .stages import regrade
    s, conn, _ = _ctx(require_contact=False)
    res = regrade.run(conn, s, Path(args.before) if args.before else None)
    if res["failed"]:
        sys.exit(f"{res['failed']} test(s) failed: export will refuse to run until they pass.")


def cmd_test_report(args):
    from . import testset
    from .stages import score
    s, conn, _ = _ctx(require_contact=False)
    score.run(conn, s)
    results = testset.run_all(conn)
    print(testset.summary(results))
    print(f"Report: {testset.write_report(results, s.out_dir)}")
    if any(r["status"] == "FAIL" for r in results):
        sys.exit(1)


def cmd_explain(args):
    from .stages import regrade
    s, conn, _ = _ctx(require_contact=False)
    for name in args.names:
        print(regrade.explain(conn, name))


def cmd_sample(args):
    from .stages import regrade
    s, conn, _ = _ctx(require_contact=False)
    print(f"Wrote {regrade.sample(conn, s, args.n, args.seed)}")


def cmd_merge(args):
    from .merge import merge
    s, conn, _ = _ctx(require_contact=False)
    try:
        merge(conn, args.keep, args.drop)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"Merged #{args.drop} into #{args.keep}. Run regrade or export to re-score.")


def cmd_import_decisions(args):
    from .stages import decisions
    s, conn, _ = _ctx(require_contact=False)
    print(f"Imported: {decisions.run(conn, Path(args.file))}")


def cmd_run(args):
    """screen -> fetch -> extract -> score -> export, for whatever is pending."""
    for fn in (cmd_screen, cmd_fetch, cmd_extract, cmd_export):
        fn(args)


def cmd_status(args):
    s, conn, _ = _ctx(require_contact=False)
    print("Universes:")
    for u in conn.execute("SELECT family, status, COUNT(*) n, SUM(member_count) m FROM universe GROUP BY family, status"):
        print(f"  {u['family']:14} {u['status']:8} {u['n']:4} lists, {u['m'] or 0} people")
    f = conn.execute(
        "SELECT COUNT(*) total, SUM(screen_status='pending') sp, SUM(screen_status='HIT') hit, "
        "SUM(screen_status='MAYBE') maybe, SUM(screen_status='MISS') miss, "
        "SUM(screen_status IN ('HIT','MAYBE') AND fetch_status='pending') fp, "
        "SUM(fetch_status='done' AND extract_status='pending') ep FROM person").fetchone()
    print(f"People: {f['total']} | to screen {f['sp'] or 0} | HIT {f['hit'] or 0} | MAYBE {f['maybe'] or 0} | "
          f"MISS {f['miss'] or 0} | to fetch {f['fp'] or 0} | to extract {f['ep'] or 0}")
    print("Priorities:", dict(conn.execute(
        "SELECT priority, COUNT(*) FROM person WHERE priority IS NOT NULL GROUP BY priority").fetchall()))
    day = conn.execute("SELECT COUNT(*) n FROM llm_call WHERE created_day=date('now','localtime')").fetchone()["n"]
    month = conn.execute("SELECT COUNT(*) n FROM search_call WHERE created_month=strftime('%Y-%m','now','localtime')").fetchone()["n"]
    print(f"Free-tier usage: LLM calls today {day}/{s.llm_daily_cap}; searches this month {month}/{s.search_monthly_cap}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m engine", description="WBS Alumni Impact Engine")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check configuration, Wikidata IDs and API keys")
    d.add_argument("--llm", action="store_true", help="also make one test LLM call")
    d.set_defaults(fn=cmd_doctor)

    i = sub.add_parser("ingest", help="load a universe of people from Wikidata")
    i.add_argument("family", choices=["seed", "political", "business", "university", "igo", "central_bank"])
    i.add_argument("--countries", help="comma-separated ISO codes, e.g. NG,GH,KE (default: starter list)")
    i.add_argument("--all", action="store_true", help="every sovereign state")
    i.add_argument("--refresh", action="store_true", help="re-run universes already ingested")
    i.set_defaults(fn=cmd_ingest)

    c = sub.add_parser("ingest-csv", help="load a hand-made list (name, role, organisation, country, source_url)")
    c.add_argument("file")
    c.add_argument("--universe", required=True, help="short name, e.g. wbs_alumni_stories")
    c.add_argument("--family", default="manual", choices=["manual", "alumni_first", "political", "business",
                                                           "igo", "central_bank", "university"])
    c.add_argument("--refresh", action="store_true")
    c.set_defaults(fn=cmd_ingest_csv)

    pv = sub.add_parser("ingest-previous", help="load the previous NebulaOne output for re-verification")
    pv.add_argument("file")
    pv.add_argument("--refresh", action="store_true")
    pv.set_defaults(fn=cmd_ingest_previous)

    for name, fn, helptext in [("screen", cmd_screen, "cheap Warwick test for every pending person"),
                               ("fetch", cmd_fetch, "collect pages for HIT/MAYBE people"),
                               ("extract", cmd_extract, "extract quote-verified claims"),
                               ("run", cmd_run, "screen, fetch, extract, score and export")]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--limit", type=int, help="process at most N people this time")
        if name == "run":
            sp.add_argument("--template", default=str(DEFAULT_TEMPLATE))
            sp.add_argument("--out")
        sp.set_defaults(fn=fn)

    sc = sub.add_parser("score", help="recompute grades, fit and priorities")
    sc.set_defaults(fn=cmd_score)
    e = sub.add_parser("export", help="write the Excel workbook")
    e.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    e.add_argument("--out")
    e.set_defaults(fn=cmd_export)
    rg = sub.add_parser("regrade", help="re-check saved quotes with the current rules and show what changed")
    rg.add_argument("--before", help="an earlier exported workbook to compare against")
    rg.set_defaults(fn=cmd_regrade)
    tr = sub.add_parser("test-report", help="run the test set and whole-dataset checks")
    tr.set_defaults(fn=cmd_test_report)
    ex = sub.add_parser("explain", help="show every quote and decision behind one or more people")
    ex.add_argument("names", nargs="+")
    ex.set_defaults(fn=cmd_explain)
    sm = sub.add_parser("sample", help="random Confirmed/Probable rows with their quotes, for checking by hand")
    sm.add_argument("--n", type=int, default=20)
    sm.add_argument("--seed", type=int)
    sm.set_defaults(fn=cmd_sample)
    mg = sub.add_parser("merge", help="merge a duplicate record into another (KEEP_ID DROP_ID)")
    mg.add_argument("keep", type=int)
    mg.add_argument("drop", type=int)
    mg.set_defaults(fn=cmd_merge)
    dcs = sub.add_parser("import-decisions", help="read reviewer edits from an exported workbook")
    dcs.add_argument("file")
    dcs.set_defaults(fn=cmd_import_decisions)
    st = sub.add_parser("status", help="show progress and free-tier usage")
    st.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.fn(args)


if __name__ == "__main__":
    main()
