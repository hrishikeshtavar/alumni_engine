"""An offline stand-in for the network, so the whole pipeline can be tested without any API.

All people, countries and organisations here are invented.
"""

from __future__ import annotations

import json

from engine.http import Response

WARWICK = [("Q1000", "University of Warwick"), ("Q1001", "Warwick Business School"),
           ("Q1002", "Warwick Manufacturing Group")]


def _b(**kw):
    return {k: {"value": (f"http://www.wikidata.org/entity/{v}" if str(v).startswith("Q") else v)}
            for k, v in kw.items() if v is not None}


def _p69(*qids):
    return [{"mainsnak": {"datavalue": {"value": {"id": q}}}, "qualifiers": {}} for q in qids]


ENTITIES = {
    "Q500": {"labels": {"en": {"value": "Ama Mensah-Doe"}}, "claims": {}, "sitelinks": {"enwiki": {"title": "Ama Mensah-Doe"}},
             "descriptions": {"en": {"value": "politician"}}},
    "Q501": {"labels": {"en": {"value": "Alumna Seed"}}, "claims": {"P69": _p69("Q1001")}, "sitelinks": {}},
    "Q502": {"labels": {"en": {"value": "Kofi Namesake"}}, "claims": {}, "sitelinks": {"enwiki": {"title": "Kofi Namesake"}}},
    "Q503": {"labels": {"en": {"value": "Grace Otherdept"}}, "claims": {"P69": _p69("Q1002")},
             "sitelinks": {"enwiki": {"title": "Grace Otherdept"}}},
    "Q504": {"labels": {"en": {"value": "Ben Fix"}}, "claims": {}, "sitelinks": {"enwiki": {"title": "Ben Fix"}}},
}

WIKI = {
    "Ama Mensah-Doe": ("Ama Mensah-Doe is a politician from Country X. She holds an MBA from Warwick Business School. "
                       "She founded PayCo in 2012.",
                       ["https://www.example.gov.xx/ministers/mensah-doe", "https://www.wbs.ac.uk/news/mensah-doe",
                        "https://www.linkedin.com/in/amamensahdoe"]),
    "Kofi Namesake": ("Kofi Namesake is a politician. He studied at the University of Lagos.", []),
    "Grace Otherdept": ("Grace Otherdept is an engineer. She studied engineering at the Warwick Manufacturing Group, "
                        "University of Warwick.", []),
    "Ben Fix": ("Ben Fix is a minister. He graduated with an MBA from the University of Warwick in 2001.",
                ["https://www.wbs.ac.uk/alumni/ben-fix"]),
}

PAGES = {
    "https://www.example.gov.xx/ministers/mensah-doe":
        "<html><head><title>Hon. Dr Ama Mensah-Doe</title></head><body><article><p>Hon. Dr Ama Mensah-Doe is the "
        "Minister of Digital Economy since 2023.</p><p>She holds an MBA from Warwick Business School and a BSc from "
        "the University of Country X.</p></article></body></html>",
    "https://www.wbs.ac.uk/news/mensah-doe":
        "<html><head><title>WBS alumna appointed minister</title></head><body><article><p>Warwick MBA alumna "
        "Ama Mensah-Doe has been appointed Minister of Digital Economy.</p></article></body></html>",
    "https://www.wbs.ac.uk/alumni/ben-fix":
        "<html><head><title>Ben Fix</title></head><body><article><p>Ben Fix completed the Warwick Business School "
        "Executive MBA in 2001 and is now a government minister.</p></article></body></html>",
}

FAKE_LLM = {
    "education": [
        {"institution": "Warwick Business School", "school_or_department": None, "programme": "MBA", "degree": "MBA",
         "year": None, "quote": "She holds an MBA from Warwick Business School"},
        {"institution": "University of Oxford", "programme": None, "degree": "DPhil", "year": "1999",
         "quote": "She completed a DPhil at Oxford in 1999"},  # invented: not on the page, must be dropped
    ],
    "roles": [{"title": "Minister of Digital Economy", "organisation": "Government of Country X", "start": "2023",
               "end": None, "is_current": True, "quote": "Minister of Digital Economy since 2023"}],
    "awards": [],
    "about_this_person": True,
    "publication_date": None,
}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get_json(self, url, params=None, headers=None, cache=True):
        params = params or {}
        self.calls.append(url)
        if "wikidata.org/w/api.php" in url and params.get("action") == "wbsearchentities":
            return {"search": [{"id": q_, "label": l, "description": ""} for q_, l in WARWICK
                               if l.lower() == params["search"].lower()]}
        if "wikidata.org/w/api.php" in url:
            ids = params["ids"].split("|")
            return {"entities": {i: ENTITIES.get(i, {"labels": {"en": {"value": "Country X"}}}) for i in ids}}
        if "wikipedia.org/w/api.php" in url:
            if params.get("list") == "search":
                return {"query": {"search": []}}
            title = params["titles"]
            if title not in WIKI:
                return {"query": {"pages": [{"missing": True}]}}
            text, links = WIKI[title]
            return {"query": {"pages": [{"title": title, "fullurl": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                                         "extract": text, "extlinks": [{"url": u} for u in links]}]}}
        raise AssertionError(f"unexpected GET {url}")

    def post_form(self, url, data, headers=None, cache=True):
        assert "query.wikidata.org" in url, url
        q = data["query"]
        self.calls.append(url)
        if "wdt:P361" in q and "wd:Q1000" in q:
            return {"results": {"bindings": [_b(org=q_, orgLabel=l) for q_, l in WARWICK[1:]]}}
        if "wdt:P576" in q:
            return {"results": {"bindings": [_b(c="Q900", cLabel="Country X", iso="XX")]}}
        if "pq:P512" in q:
            return {"results": {"bindings": [_b(person="Q501", personLabel="Alumna Seed", inst="Q1001",
                                                instLabel="Warwick Business School")]}}
        if "wdt:P279* ?root" in q:
            return {"results": {"bindings": [_b(cls="Q83307")]}}
        if "?pos wdt:P31 ?cls" in q:
            return {"results": {"bindings": [_b(pos=p_, j="Q900") for p_ in ("Q700", "Q701", "Q702", "Q703")]
                                + [_b(pos="Q799", j="Q901")]}}  # a state-level office elsewhere: ignored
        if "VALUES ?pos" in q and "ps:P39" not in q:
            # office modelled as a sub-class (like 'Prime Minister of the United Kingdom')
            return {"results": {"bindings": [_b(pos="Q83307_SUB", j="Q900")] if "wd:Q83307" in q else []}}
        if "VALUES ?pos" in q:
            rows = [
                _b(person="Q500", personLabel="Ama Mensah-Doe", pos="Q700", posLabel="Minister of Digital Economy",
                   start="2023-01-10T00:00:00Z"),
                _b(person="Q502", personLabel="Kofi Namesake", pos="Q701", posLabel="Minister of Finance",
                   start="2020-01-01T00:00:00Z", end="2022-01-01T00:00:00Z"),
                _b(person="Q503", personLabel="Grace Otherdept", pos="Q702", posLabel="Minister of Industry",
                   start="2021-05-01T00:00:00Z"),
                _b(person="Q504", personLabel="Ben Fix", pos="Q703", posLabel="Minister of Trade",
                   start="2024-02-01T00:00:00Z"),
            ]
            return {"results": {"bindings": [r for r in rows if f"wd:{r['pos']['value'].rsplit('/', 1)[-1]}" in q]}}
        return {"results": {"bindings": []}}

    def post_json(self, url, body, headers=None, cache=True, cache_extra=""):
        if "generativelanguage" in url:
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(FAKE_LLM)}]}}]}
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, max_bytes=3_000_000):
        if url.endswith("/robots.txt"):
            return Response(404, "", url, "text/plain")
        if url in PAGES:
            return Response(200, PAGES[url], url, "text/html; charset=utf-8")
        return Response(404, "", url, "text/html")
