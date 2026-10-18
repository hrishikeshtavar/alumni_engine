"""Wikidata access: SPARQL universes (finite lists of influential people) and entity lookups.

All free. Wikidata is used for DISCOVERY and for structured role dates; it is never treated
as proof of WBS identity on its own (tier 7).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SPARQL_URL = "https://query.wikidata.org/sparql"
API_URL = "https://www.wikidata.org/w/api.php"
ENTITY_URL = "https://www.wikidata.org/wiki/{}"

# Class IDs used in queries. `python -m engine doctor` checks each label before a real run.
QID = {
    "human": "Q5",
    "minister": "Q83307",
    "head_of_government": "Q2285706",
    "head_of_state": "Q48352",
    "igo": "Q245065",
    "central_bank": "Q66344",
    "university": "Q3918",
    "public_university": "Q875538",
    "sovereign_state": "Q3624078",
}
EXPECTED_LABEL = {
    "Q5": "human",
    "Q83307": "minister",
    "Q2285706": "head of government",
    "Q48352": "head of state",
    "Q245065": "intergovernmental organization",
    "Q66344": "central bank",
    "Q3918": "university",
    "Q875538": "public university",
    "Q3624078": "sovereign state",
}
LABEL_LANGS = "en,fr,es,pt,de,ar,ru,zh,ja,ko,id,tr,it,nl,sw"


def strip_entity(value: str) -> str:
    return value.rsplit("/", 1)[-1] if value and value.startswith("http://www.wikidata.org/entity/") else value


def sparql(http, query: str) -> list[dict]:
    # POST: queries with long VALUES lists are too big for a URL.
    data = http.post_form(SPARQL_URL, {"query": query, "format": "json"},
                          headers={"Accept": "application/sparql-results+json"})
    rows = []
    for b in data.get("results", {}).get("bindings", []):
        rows.append({k: strip_entity(v.get("value", "")) for k, v in b.items()})
    return rows


def entities(http, qids: list[str], props: str = "claims|sitelinks|labels|aliases|descriptions") -> dict:
    out: dict = {}
    for i in range(0, len(qids), 50):
        batch = [q for q in qids[i:i + 50] if q]
        if not batch:
            continue
        data = http.get_json(API_URL, params={
            "action": "wbgetentities", "ids": "|".join(batch), "props": props,
            "languages": "en", "languagefallback": 1, "format": "json",
        })
        out.update(data.get("entities", {}))
    return out


def labels_for(http, qids: list[str]) -> dict[str, str]:
    ents = entities(http, sorted(set(qids)), props="labels")
    result = {}
    for qid, ent in ents.items():
        lab = ent.get("labels", {})
        result[qid] = (lab.get("en") or next(iter(lab.values()), {})).get("value", qid) if lab else qid
    return result


def claim_items(entity: dict, prop: str) -> list[dict]:
    """Item values of a property with a few qualifiers: [{'qid':..., 'start':..., 'end':..., 'degree':...}]."""
    out = []
    for st in entity.get("claims", {}).get(prop, []):
        snak = st.get("mainsnak", {})
        val = snak.get("datavalue", {}).get("value")
        if not isinstance(val, dict) or "id" not in val:
            continue
        quals = st.get("qualifiers", {})

        def qtime(p):
            try:
                return quals[p][0]["datavalue"]["value"]["time"].lstrip("+")[:10]
            except (KeyError, IndexError, TypeError):
                return ""

        def qitem(p):
            try:
                return quals[p][0]["datavalue"]["value"]["id"]
            except (KeyError, IndexError, TypeError):
                return ""

        out.append({"qid": val["id"], "start": qtime("P580"), "end": qtime("P582"),
                    "degree": qitem("P512"), "major": qitem("P812")})
    return out


def entity_label(entity: dict) -> str:
    lab = entity.get("labels", {})
    if "en" in lab:
        return lab["en"]["value"]
    return next(iter(lab.values()), {}).get("value", "") if lab else ""


def entity_aliases(entity: dict) -> list[str]:
    return [a["value"] for a in entity.get("aliases", {}).get("en", [])][:8]


def pick_wiki(entity: dict) -> tuple[str, str] | tuple[None, None]:
    """English Wikipedia if present, otherwise the first other-language Wikipedia article."""
    links = entity.get("sitelinks", {})
    if "enwiki" in links:
        return "en", links["enwiki"]["title"]
    for site, link in links.items():
        if site.endswith("wiki") and site not in ("commonswiki", "specieswiki", "metawiki", "wikidatawiki") \
                and "quote" not in site and "source" not in site and "news" not in site and "books" not in site \
                and "voyage" not in site and "versity" not in site:
            return site[:-4].replace("_", "-"), link["title"]
    return None, None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
LABEL_SERVICE = 'SERVICE wikibase:label { bd:serviceParam wikibase:language "%s" . }' % LABEL_LANGS


def search_items(http, text: str, limit: int = 7) -> list[dict]:
    """Fast name lookup (wbsearchentities). Returns [{'id','label','description'}]."""
    data = http.get_json(API_URL, params={
        "action": "wbsearchentities", "search": text, "language": "en", "uselang": "en",
        "type": "item", "limit": limit, "format": "json",
    })
    return [{"id": r.get("id", ""), "label": r.get("label", ""), "description": r.get("description", "")}
            for r in data.get("search", [])]


def warwick_parts_query(warwick_qid: str) -> str:
    """Units that are part of / subsidiaries of the University of Warwick, up to two levels down.
    Deliberately avoids an open-ended '+' path, which times out on the public endpoint."""
    rel = "(wdt:P361|wdt:P749)"
    return f"""
SELECT DISTINCT ?org ?orgLabel WHERE {{
  {{ ?org {rel} wd:{warwick_qid} . }} UNION {{ ?org {rel}/{rel} wd:{warwick_qid} . }}
  {LABEL_SERVICE}
}}"""


def countries_query() -> str:
    return f"""
SELECT DISTINCT ?c ?cLabel ?iso WHERE {{
  ?c wdt:P31 wd:{QID['sovereign_state']} ; wdt:P297 ?iso .
  FILTER NOT EXISTS {{ ?c wdt:P576 ?dissolved . }}
  {LABEL_SERVICE}
}}"""


def seed_query(warwick_qids: list[str]) -> str:
    values = " ".join(f"wd:{q}" for q in warwick_qids)
    return f"""
SELECT DISTINCT ?person ?personLabel ?inst ?instLabel ?degreeLabel ?end WHERE {{
  VALUES ?inst {{ {values} }}
  ?person p:P69 ?st . ?st ps:P69 ?inst .
  ?person wdt:P31 wd:{QID['human']} .
  OPTIONAL {{ ?st pq:P512 ?degree . }}
  OPTIONAL {{ ?st pq:P582 ?end . }}
  {LABEL_SERVICE}
}}"""


def _window_filters(since: int) -> str:
    return f"""
  OPTIONAL {{ ?st pq:P580 ?start . }}
  OPTIONAL {{ ?st pq:P582 ?end . }}
  FILTER(!BOUND(?end) || YEAR(?end) >= {since})
  FILTER NOT EXISTS {{ ?person wdt:P570 ?dod . FILTER(YEAR(?dod) < {since}) }}
  FILTER(BOUND(?start) || BOUND(?end) || NOT EXISTS {{ ?person wdt:P570 ?d2 . }})"""


# ---------------------------------------------------------------------------
# Universe queries, split into small steps. A single query that starts from a whole country
# (every building, law and company in it) times out on the free endpoint, so instead:
#   1. list candidate positions / organisations with one selective pattern,
#   2. look up who held them, 100 at a time.
# ---------------------------------------------------------------------------
BATCH = 100


def values(qids) -> str:
    return " ".join(f"wd:{q}" for q in qids)


def political_classes_query() -> str:
    """Every class that is a kind of minister, head of government or head of state."""
    return f"""
SELECT DISTINCT ?cls WHERE {{
  VALUES ?root {{ wd:{QID['minister']} wd:{QID['head_of_government']} wd:{QID['head_of_state']} }}
  ?cls wdt:P279* ?root .
}}"""


def positions_of_classes_query(class_qids) -> str:
    """Positions that are instances of these classes, with their jurisdiction and country."""
    return f"""
SELECT DISTINCT ?pos ?j ?k WHERE {{
  VALUES ?cls {{ {values(class_qids)} }}
  ?pos wdt:P31 ?cls .
  OPTIONAL {{ ?pos wdt:P1001 ?j . }}
  OPTIONAL {{ ?pos wdt:P17 ?k . }}
}}"""


def jurisdictions_query(qids) -> str:
    """Jurisdiction and country of the given items (used for offices modelled as sub-classes)."""
    return f"""
SELECT DISTINCT ?pos ?j ?k WHERE {{
  VALUES ?pos {{ {values(qids)} }}
  OPTIONAL {{ ?pos wdt:P1001 ?j . }}
  OPTIONAL {{ ?pos wdt:P17 ?k . }}
  FILTER(BOUND(?j) || BOUND(?k))
}}"""


def holders_query(pos_qids, since: int) -> str:
    """People who held these positions within the window."""
    return f"""
SELECT DISTINCT ?person ?personLabel ?pos ?posLabel ?start ?end WHERE {{
  VALUES ?pos {{ {values(pos_qids)} }}
  ?person p:P39 ?st . ?st ps:P39 ?pos .
  ?person wdt:P31 wd:{QID['human']} .{_window_filters(since)}
  {LABEL_SERVICE}
}}"""


def listed_companies_query(iso: str) -> str:
    return f"""
SELECT DISTINCT ?org WHERE {{
  hint:Query hint:optimizer "None" .
  ?org wdt:P414 ?exchange .
  ?org wdt:P17 ?c .
  ?c wdt:P297 "{iso}" .
}}"""


def universities_query(iso: str) -> str:
    return f"""
SELECT DISTINCT ?org WHERE {{
  hint:Query hint:optimizer "None" .
  VALUES ?type {{ wd:{QID['university']} wd:{QID['public_university']} }}
  ?org wdt:P31 ?type .
  ?org wdt:P17 ?c .
  ?c wdt:P297 "{iso}" .
}}"""


def orgs_of_class_query(class_key: str) -> str:
    return f"""
SELECT DISTINCT ?org WHERE {{
  ?org wdt:P31/wdt:P279* wd:{QID[class_key]} .
}}"""


def org_holders_query(org_qids, since: int, with_board: bool = False) -> str:
    """Heads of these organisations: CEO, chair, director (org side) and office holders (position side)."""
    board = '(p:P3320 ps:P3320 "Board member")' if with_board else ""
    return f"""
SELECT DISTINCT ?person ?personLabel ?org ?orgLabel ?pos ?posLabel ?role ?start ?end WHERE {{
  VALUES ?org {{ {values(org_qids)} }}
  {{
    VALUES (?pp ?pps ?role) {{ (p:P169 ps:P169 "Chief executive") (p:P488 ps:P488 "Chairperson")
                               (p:P1037 ps:P1037 "Director / head") {board} }}
    ?org ?pp ?st . ?st ?pps ?person .
  }} UNION {{
    ?pos wdt:P2389 ?org . ?person p:P39 ?st . ?st ps:P39 ?pos .
  }}
  ?person wdt:P31 wd:{QID['human']} .{_window_filters(since)}
  {LABEL_SERVICE}
}}"""
