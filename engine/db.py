"""SQLite storage. The database is the only shared state; every stage reads and writes it."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS universe (
    id TEXT PRIMARY KEY,             -- e.g. political:NG, business:GB, igo:global, alumni:wikidata_seed
    family TEXT NOT NULL,            -- political | igo | central_bank | business | university | alumni_first | manual
    label TEXT,
    country TEXT,                    -- ISO alpha-2 or ''
    status TEXT DEFAULT 'pending',   -- pending | done | error
    member_count INTEGER DEFAULT 0,
    error TEXT,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY,
    qid TEXT UNIQUE,                 -- Wikidata ID when known (hard identity key)
    name TEXT NOT NULL,
    norm_name TEXT,
    aliases TEXT DEFAULT '[]',
    description TEXT,
    country TEXT,
    wiki_lang TEXT,
    wiki_title TEXT,
    wiki_links TEXT DEFAULT '[]',
    context_org TEXT,
    screen_status TEXT DEFAULT 'pending',   -- pending | HIT | MAYBE | MISS
    screen_reason TEXT,
    screen_snippet TEXT,
    fetch_status TEXT DEFAULT 'pending',
    extract_status TEXT DEFAULT 'pending',
    identity_grade TEXT,
    identity_note TEXT,
    qs_fit TEXT,
    qs_family TEXT,
    attribution INTEGER,
    impact_level INTEGER,
    stream TEXT,
    category TEXT,
    priority TEXT,
    submission_status TEXT,
    confidence TEXT,
    next_action TEXT,
    current_role TEXT,
    current_org TEXT,
    role_as_of TEXT,
    wbs_employee INTEGER DEFAULT 0,
    decision TEXT,
    decision_note TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_person_norm ON person(norm_name);
CREATE INDEX IF NOT EXISTS ix_person_screen ON person(screen_status);

CREATE TABLE IF NOT EXISTS membership (
    person_id INTEGER NOT NULL,
    universe_id TEXT NOT NULL,
    role_title TEXT NOT NULL DEFAULT '',
    role_org TEXT NOT NULL DEFAULT '',
    start TEXT NOT NULL DEFAULT '',
    "end" TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (person_id, universe_id, role_title, role_org, start)
);

CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE,
    domain TEXT,
    title TEXT,
    tier INTEGER,
    source_type TEXT,
    warwick_side INTEGER DEFAULT 0,
    published_at TEXT,
    accessed_at TEXT,
    text_path TEXT,
    status TEXT                      -- ok | irrelevant | blocked | robots | error | structured
);

CREATE TABLE IF NOT EXISTS person_source (
    person_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    via TEXT,
    PRIMARY KEY (person_id, source_id)
);

CREATE TABLE IF NOT EXISTS claim (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL,
    claim_type TEXT NOT NULL,        -- education | role | award
    data TEXT,                       -- JSON
    dept TEXT,                       -- education only: WBS | OTHER | GENERIC
    quote TEXT,
    source_id INTEGER,
    verified INTEGER DEFAULT 0,
    origin TEXT,                     -- wikidata | rule | llm
    created_at TEXT,
    UNIQUE (person_id, claim_type, source_id, quote)
);

CREATE TABLE IF NOT EXISTS http_cache (key TEXT PRIMARY KEY, status INTEGER, body TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS llm_call (key TEXT PRIMARY KEY, model TEXT, response TEXT, created_day TEXT);
CREATE TABLE IF NOT EXISTS search_call (key TEXT PRIMARY KEY, provider TEXT, query TEXT, response TEXT, created_month TEXT);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY, command TEXT, started_at TEXT, finished_at TEXT, processed INTEGER, notes TEXT
);
"""


# Order in which people are fetched and extracted, so free-tier credits go to the strongest
# candidates first: WBS named, then people in a QS-type role, then the previous run, then the rest.
PROCESS_ORDER = (
    " ORDER BY CASE WHEN screen_status='HIT' THEN 0"
    " WHEN EXISTS (SELECT 1 FROM membership m JOIN universe u ON u.id=m.universe_id"
    "   WHERE m.person_id=person.id AND u.family IN ('political','igo','central_bank','business','university')) THEN 1"
    " WHEN EXISTS (SELECT 1 FROM membership m WHERE m.person_id=person.id"
    "   AND m.universe_id='alumni_first:previous_nebula') THEN 2"
    " ELSE 3 END, id"
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def today() -> str:
    return time.strftime("%Y-%m-%d")


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release, so existing databases keep working."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(claim)")}
    for col in ("claim_class", "rejected_reason"):
        if col not in have:
            conn.execute(f"ALTER TABLE claim ADD COLUMN {col} TEXT")
    have_llm = {r["name"] for r in conn.execute("PRAGMA table_info(llm_call)")}
    for col in ("provider", "run_id"):
        if col not in have_llm:
            conn.execute(f"ALTER TABLE llm_call ADD COLUMN {col} TEXT")
    have_claim = {r["name"] for r in conn.execute("PRAGMA table_info(claim)")}
    if "rejected_detail" not in have_claim:
        conn.execute("ALTER TABLE claim ADD COLUMN rejected_detail TEXT")
    have_p = {r["name"] for r in conn.execute("PRAGMA table_info(person)")}
    for col in ("former_wbs_staff", "impact_marker"):
        if col not in have_p:
            conn.execute(f"ALTER TABLE person ADD COLUMN {col} TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS grade_snapshot (person_id INTEGER PRIMARY KEY, name TEXT, "
                 "identity_grade TEXT, priority TEXT, submission_status TEXT, taken_at TEXT)")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def log_run(conn: sqlite3.Connection, command: str, started: str, processed: int, notes: str = "") -> None:
    conn.execute(
        "INSERT INTO run_log(command, started_at, finished_at, processed, notes) VALUES (?,?,?,?,?)",
        (command, started, now(), processed, notes),
    )
    conn.commit()
