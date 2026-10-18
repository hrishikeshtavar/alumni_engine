"""Polite HTTP client: identifies itself, rate-limits per host, retries, and caches JSON API calls in SQLite."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx

log = logging.getLogger(__name__)

# Seconds between requests to the same host. Wikimedia allows more, but there is no hurry.
MIN_INTERVAL = {
    "query.wikidata.org": 1.0,
    "www.wikidata.org": 0.2,
    "wikipedia.org": 0.2,
    "generativelanguage.googleapis.com": 0.0,  # the LLM wrapper does its own pacing
    "api.tavily.com": 0.5,
    "google.serper.dev": 0.5,
}
DEFAULT_INTERVAL = 2.0
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_PAGE_BYTES = 3_000_000


@dataclass
class Response:
    status: int
    text: str
    url: str
    content_type: str


class Http:
    def __init__(self, conn: sqlite3.Connection, user_agent: str, cache_days: int = 30):
        self.conn = conn
        self.cache_seconds = cache_days * 86400
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en,*;q=0.5"},
            timeout=httpx.Timeout(45.0, connect=15.0),
            follow_redirects=True,
        )
        self._last: dict[str, float] = {}

    # -- pacing -------------------------------------------------------------
    def _wait(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        interval = DEFAULT_INTERVAL
        for key, value in MIN_INTERVAL.items():
            if host == key or host.endswith("." + key) or host.endswith(key):
                interval = value
                break
        elapsed = time.monotonic() - self._last.get(host, 0.0)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last[host] = time.monotonic()

    def _request(self, method: str, url: str, page: bool = False, **kw) -> httpx.Response:
        # APIs (Wikidata, Wikipedia, Gemini, search) get patient retries. Ordinary web pages get one
        # quick retry at most: a dead or broken website will not come back by waiting.
        delays = [3] if page else [2, 6, 20, 45]
        timeouts = 0
        if page:
            kw.setdefault("timeout", httpx.Timeout(20.0, connect=8.0))
        if "query.wikidata.org" in url:
            # Wikidata's public endpoint stops queries at 60s; wait just past that, not longer.
            kw.setdefault("timeout", httpx.Timeout(70.0, connect=15.0))
        for attempt in range(len(delays) + 1):
            self._wait(url)
            try:
                resp = self.client.request(method, url, **kw)
            except httpx.TimeoutException:
                timeouts += 1
                if page or timeouts >= 2 or attempt == len(delays):
                    raise  # a query that timed out twice will time out again; fail fast and move on
                log.warning("timeout on %s; retrying once", urlparse(url).hostname)
                time.sleep(delays[attempt])
                continue
            except httpx.ConnectError:
                # DNS failure, refused connection or bad TLS certificate: permanent, never retried.
                raise
            except httpx.TransportError as exc:
                if attempt == len(delays):
                    raise
                log.warning("network error on %s (%s); retrying", url, exc)
                time.sleep(delays[attempt])
                continue
            if resp.status_code in RETRY_STATUS and attempt < len(delays):
                retry_after = resp.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else delays[attempt]
                log.warning("HTTP %s from %s; waiting %ss", resp.status_code, urlparse(url).hostname, wait)
                time.sleep(min(wait, 120))
                continue
            return resp
        raise RuntimeError("unreachable")

    # -- cache --------------------------------------------------------------
    def _cache_get(self, key: str):
        row = self.conn.execute("SELECT status, body, fetched_at FROM http_cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < self.cache_seconds and row["status"] == 200:
            return json.loads(row["body"])
        return None

    def _cache_put(self, key: str, status: int, body) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO http_cache(key, status, body, fetched_at) VALUES (?,?,?,?)",
            (key, status, json.dumps(body), time.time()),
        )
        self.conn.commit()

    @staticmethod
    def _key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

    # -- public API ---------------------------------------------------------
    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None, cache: bool = True):
        key = self._key("GET", url, params or {})
        if cache:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        resp = self._request("GET", url, params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if cache:
            self._cache_put(key, resp.status_code, body)
        return body

    def post_json(self, url: str, body: dict, headers: dict | None = None, cache: bool = True,
                  cache_extra: str = ""):
        key = self._key("POST", url, body, cache_extra)
        if cache:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        resp = self._request("POST", url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{resp.status_code} from {urlparse(url).hostname}: {resp.text[:300]}",
                request=resp.request, response=resp,
            )
        data = resp.json()
        if cache:
            self._cache_put(key, resp.status_code, data)
        return data

    def post_form(self, url: str, data: dict, headers: dict | None = None, cache: bool = True):
        key = self._key("POSTFORM", url, data)
        if cache:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        resp = self._request("POST", url, data=data, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if cache:
            self._cache_put(key, resp.status_code, body)
        return body

    def get(self, url: str, max_bytes: int = MAX_PAGE_BYTES) -> Response:
        """Fetch a web page (not cached here; page text is stored by the fetch stage)."""
        resp = self._request("GET", url, page=True)
        ctype = resp.headers.get("content-type", "").lower()
        content = resp.content[:max_bytes]
        try:
            text = content.decode(resp.encoding or "utf-8", errors="replace")
        except LookupError:
            text = content.decode("utf-8", errors="replace")
        return Response(resp.status_code, text, str(resp.url), ctype)

    def close(self) -> None:
        self.client.close()


def url_with_params(url: str, params: dict) -> str:
    return f"{url}?{urlencode(params)}"
