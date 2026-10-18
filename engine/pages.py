"""Fetch a public web page politely (robots.txt respected) and keep only its readable text."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

_robots: dict[str, robotparser.RobotFileParser | None] = {}
_dead_hosts: set[str] = set()  # sites that could not be reached at all during this run


def robots_allows(http, url: str, user_agent: str) -> bool:
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            resp = http.get(base + "/robots.txt", max_bytes=500_000)
            if resp.status == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # no robots.txt: allowed
        except httpx.ConnectError:
            _dead_hosts.add(parts.netloc)  # the site does not exist or refuses connections
            rp = None
        except Exception:  # noqa: BLE001
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(user_agent, url)


def html_to_text(html: str) -> tuple[str, str, str]:
    """Return (text, title, date)."""
    title, date, text = "", "", ""
    if trafilatura is not None:
        text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True) or ""
        try:
            meta = trafilatura.extract_metadata(html)
            if meta is not None:
                title = meta.title or ""
                date = meta.date or ""
        except Exception:  # noqa: BLE001
            pass
    if not text:
        cleaned = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
        cleaned = re.sub(r"(?s)<[^>]+>", "\n", cleaned)
        text = re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", cleaned)).strip()
    if not title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return text, title[:300], date[:10]


def save_text(pages_dir: Path, url: str, text: str) -> str:
    pages_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt"
    path = pages_dir / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def fetch(http, url: str, user_agent: str) -> dict:
    """Return {'status': ok|robots|error|not_html, 'text', 'title', 'date', 'final_url'}."""
    if urlparse(url).netloc in _dead_hosts:
        return {"status": "error"}
    if not robots_allows(http, url, user_agent):
        return {"status": "robots"}
    if urlparse(url).netloc in _dead_hosts:
        log.info("skipping unreachable site %s", urlparse(url).netloc)
        return {"status": "error"}
    try:
        resp = http.get(url)
    except Exception as exc:  # noqa: BLE001
        log.info("fetch failed %s: %s", url, exc)
        return {"status": "error"}
    if resp.status != 200:
        return {"status": "error"}
    if resp.content_type and not any(t in resp.content_type for t in ("html", "text/plain", "xml")):
        return {"status": "not_html"}
    text, title, date = html_to_text(resp.text)
    return {"status": "ok", "text": text, "title": title, "date": date, "final_url": resp.url}
