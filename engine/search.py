"""Optional web search on a free tier (Tavily or Serper), with a hard monthly cap.

Search results are only used to find pages to fetch. Snippets are never stored as evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

log = logging.getLogger(__name__)


class SearchBudgetExhausted(Exception):
    pass


class Search:
    def __init__(self, conn, http, settings):
        self.conn, self.http, self.s = conn, http, settings
        self.provider = settings.search_provider
        key = {"tavily": settings.tavily_api_key, "serper": settings.serper_api_key}.get(self.provider, "")
        self.key = key
        self.available = bool(key) and self.provider in ("tavily", "serper")

    def used_this_month(self) -> int:
        month = time.strftime("%Y-%m")
        row = self.conn.execute("SELECT COUNT(*) n FROM search_call WHERE created_month=? AND provider=?",
                                (month, self.provider)).fetchone()
        return row["n"]

    def urls(self, query: str) -> list[tuple[str, str]]:
        if not self.available:
            return []
        key = hashlib.sha256(f"{self.provider}|{query}".encode()).hexdigest()
        row = self.conn.execute("SELECT response FROM search_call WHERE key=?", (key,)).fetchone()
        if row:
            return [tuple(x) for x in json.loads(row["response"])]
        if self.used_this_month() >= self.s.search_monthly_cap:
            raise SearchBudgetExhausted(f"monthly search cap of {self.s.search_monthly_cap} reached")
        if self.provider == "tavily":
            data = self.http.post_json("https://api.tavily.com/search",
                                       {"query": query, "max_results": 8, "search_depth": "basic"},
                                       headers={"Authorization": f"Bearer {self.key}"}, cache=False)
            results = [(r.get("url", ""), r.get("title", "")) for r in data.get("results", [])]
        else:
            data = self.http.post_json("https://google.serper.dev/search", {"q": query, "num": 10},
                                       headers={"X-API-KEY": self.key}, cache=False)
            results = [(r.get("link", ""), r.get("title", "")) for r in data.get("organic", [])]
        results = [r for r in results if r[0]]
        self.conn.execute(
            "INSERT OR REPLACE INTO search_call(key, provider, query, response, created_month) VALUES (?,?,?,?,?)",
            (key, self.provider, query, json.dumps(results), time.strftime("%Y-%m")),
        )
        self.conn.commit()
        return results
