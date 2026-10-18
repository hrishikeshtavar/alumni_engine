"""LLM access behind one provider interface, chosen in .env (LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, LLM_BASE_URL),
so the engine can use whichever model the university's data-governance review approves without code changes.

What is sent to a model: the person's public name, public role context (from Wikidata or public lists) and the text
of ONE public web page. No WBS or university records are ever sent. Every call is cached and logged with its
provider, model and run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    pass


class DailyCapReached(Exception):
    pass


class Provider:
    """One model API. Add a subclass to support another provider; nothing else in the engine changes."""
    name = "base"

    def __init__(self, http, settings):
        self.http, self.s = http, settings
        self.model = settings.llm_model

    @property
    def configured(self) -> bool:
        return bool(self.s.llm_api_key and self.model)

    def complete_json(self, prompt: str) -> str:
        raise NotImplementedError


class GeminiProvider(Provider):
    name = "gemini"

    def complete_json(self, prompt: str) -> str:
        base = self.s.llm_base_url or "https://generativelanguage.googleapis.com/v1beta"
        data = self.http.post_json(
            f"{base}/models/{self.model}:generateContent",
            {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
             "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
            headers={"x-goog-api-key": self.s.llm_api_key}, cache=False)
        return data["candidates"][0]["content"]["parts"][0]["text"]


class OpenAICompatibleProvider(Provider):
    """OpenAI's Chat Completions format, used by many providers and gateways. Set LLM_BASE_URL for anything other
    than OpenAI itself, and LLM_AUTH_HEADER=api-key for gateways that expect that header instead of a Bearer token."""
    name = "openai"

    def complete_json(self, prompt: str) -> str:
        base = self.s.llm_base_url or "https://api.openai.com/v1"
        if self.s.llm_auth_header:
            headers = {self.s.llm_auth_header: self.s.llm_api_key}
        else:
            headers = {"Authorization": f"Bearer {self.s.llm_api_key}"}
        data = self.http.post_json(
            f"{base}/chat/completions",
            {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
             "messages": [{"role": "user", "content": prompt}]},
            headers=headers, cache=False)
        return data["choices"][0]["message"]["content"]


class AnthropicProvider(Provider):
    name = "anthropic"

    def complete_json(self, prompt: str) -> str:
        base = self.s.llm_base_url or "https://api.anthropic.com/v1"
        data = self.http.post_json(
            f"{base}/messages",
            {"model": self.model, "max_tokens": 4096, "temperature": 0,
             "messages": [{"role": "user", "content": prompt}]},
            headers={"x-api-key": self.s.llm_api_key, "anthropic-version": "2023-06-01"}, cache=False)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


PROVIDERS = {p.name: p for p in (GeminiProvider, OpenAICompatibleProvider, AnthropicProvider)}


class LLM:
    """Every model call in the engine goes through here: provider chosen by config, cached, paced, capped, logged."""

    def __init__(self, conn, http, settings, run_id: str | None = None):
        self.conn, self.http, self.s = conn, http, settings
        cls = PROVIDERS.get(settings.llm_provider)
        self.provider = cls(http, settings) if cls else None
        self.model = self.provider.model if self.provider else ""
        self.provider_name = settings.llm_provider
        self.available = bool(self.provider and self.provider.configured)
        self.run_id = run_id or time.strftime("%Y-%m-%dT%H:%M:%S")
        self.calls_made = 0
        self._last_call = 0.0

    def describe(self) -> str:
        return f"{self.provider_name} / {self.model}" if self.available else "none (rule-based extraction only)"

    def calls_today(self) -> int:
        day = time.strftime("%Y-%m-%d")
        return self.conn.execute("SELECT COUNT(*) n FROM llm_call WHERE created_day=? AND model=?",
                                 (day, self.model)).fetchone()["n"]

    def json(self, prompt: str) -> dict:
        if not self.available:
            raise LLMUnavailable("no LLM configured")
        key = hashlib.sha256(f"{self.provider_name}|{self.model}|{prompt}".encode()).hexdigest()
        row = self.conn.execute("SELECT response FROM llm_call WHERE key=?", (key,)).fetchone()
        if row:
            return json.loads(row["response"])
        if self.calls_today() >= self.s.llm_daily_cap:
            raise DailyCapReached(f"daily LLM cap of {self.s.llm_daily_cap} reached; re-run tomorrow")

        gap = 60.0 / self.s.llm_rpm
        wait = gap - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        try:
            text = self.provider.complete_json(prompt)
        except (KeyError, IndexError, TypeError) as exc:
            log.warning("unexpected %s response shape: %s", self.provider_name, exc)
            text = "{}"
        except Exception as exc:  # noqa: BLE001
            if "429" in str(exc) or "quota" in str(exc).lower() or "rate" in str(exc).lower():
                raise DailyCapReached(f"{self.provider_name} quota or rate limit reached for now") from exc
            raise
        parsed = parse_json(text)
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_call(key, model, response, created_day, provider, run_id) VALUES (?,?,?,?,?,?)",
            (key, self.model, json.dumps(parsed), time.strftime("%Y-%m-%d"), self.provider_name, self.run_id),
        )
        self.conn.commit()
        self.calls_made += 1
        return parsed


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"items": value}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {}


EXTRACT_PROMPT = """You extract facts about ONE named person from a web page, for an evidence database.

Person: {name}
Known context: {context}
Source URL: {url}

Rules:
- Only extract facts that the page states about THIS person. If the page is about someone else, set "about_this_person" to false and return empty lists.
- Every item MUST include "quote": an exact, contiguous, word-for-word copy of the sentence or phrase from the page that states it. Do not paraphrase, translate, or fix spelling in the quote.
- If a field is not stated, use null. Never guess. Empty lists are fine.
- Education: list every institution the page says the person studied at. Record the institution, school or department, programme, degree and year exactly as stated.
- Roles: senior positions, appointments and board seats, with dates if stated.
- Awards: honours, prizes, rankings and list memberships.

Return JSON only, in this shape:
{{"about_this_person": true,
  "publication_date": "YYYY-MM-DD or null",
  "education": [{{"institution": "", "school_or_department": null, "programme": null, "degree": null, "year": null, "quote": ""}}],
  "roles": [{{"title": "", "organisation": "", "start": null, "end": null, "is_current": null, "quote": ""}}],
  "awards": [{{"name": "", "year": null, "quote": ""}}]}}

Page text:
\"\"\"
{text}
\"\"\"
"""
