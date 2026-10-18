"""Wikipedia: plain-text article plus the external links it cites (discovery only, tier 7)."""

from __future__ import annotations

from rapidfuzz import fuzz

from .names import normalize_name


def api_url(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


def page(http, lang: str, title: str) -> dict | None:
    """Return {'title','url','text','links'} or None. Not cached: screening results are stored instead."""
    data = http.get_json(api_url(lang), params={
        "action": "query", "prop": "extracts|extlinks|info", "explaintext": 1, "ellimit": "max",
        "inprop": "url", "titles": title, "redirects": 1, "format": "json", "formatversion": 2,
    }, cache=False)
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    p = pages[0]
    return {
        "title": p.get("title", title),
        "url": p.get("fullurl") or f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}",
        "text": p.get("extract", "") or "",
        "links": [l.get("url", "") for l in p.get("extlinks", []) if l.get("url")],
    }


def find_title(http, name: str, hint: str = "Warwick") -> str | None:
    """Search English Wikipedia for a person by name; accept only a close title match."""
    data = http.get_json(api_url("en"), params={
        "action": "query", "list": "search", "srsearch": f'"{name}" {hint}', "srlimit": 5,
        "format": "json", "formatversion": 2,
    })
    target = normalize_name(name)
    for hit in data.get("query", {}).get("search", []):
        title = hit.get("title", "")
        if fuzz.token_sort_ratio(normalize_name(title.split("(")[0]), target) >= 92:
            return title
    return None
