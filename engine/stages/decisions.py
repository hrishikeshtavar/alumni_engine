"""IMPORT-DECISIONS: read reviewer edits back from an exported workbook so later runs keep them."""

from __future__ import annotations

import warnings
from pathlib import Path

import openpyxl

from ..db import now
from .score import submission_status

STATUSES = {"Submit", "Consider", "Hold", "Do not submit"}


def run(conn, path: Path) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    stats = {"rows": 0, "decisions": 0, "comments": 0}
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header or "Engine person ID" not in header:
            continue
        idx = {h: i for i, h in enumerate(header) if h}

        def cell(row, col):
            i = idx.get(col)
            return row[i] if i is not None and i < len(row) and row[i] is not None else ""

        for r in rows:
            pid = cell(r, "Engine person ID")
            if not str(pid).strip().isdigit():
                continue
            p = conn.execute("SELECT * FROM person WHERE id=?", (int(pid),)).fetchone()
            if not p:
                continue
            stats["rows"] += 1
            status = str(cell(r, "Suggested submission status")).strip()
            comment = str(cell(r, "Reviewer comments")).strip()
            engine_status = submission_status(p["priority"] or "", p["identity_grade"] or "")
            decision = status if status in STATUSES and status != engine_status else p["decision"]
            if decision != p["decision"]:
                stats["decisions"] += 1
            if comment:
                stats["comments"] += 1
            conn.execute("UPDATE person SET decision=?, decision_note=?, updated_at=? WHERE id=?",
                         (decision, comment or p["decision_note"], now(), p["id"]))
    conn.commit()
    return stats
