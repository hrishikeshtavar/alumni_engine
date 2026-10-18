"""Settings, read from environment variables (and a .env file if present)."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    contact_email: str
    window_years: int
    llm_provider: str
    gemini_api_key: str
    gemini_model: str
    llm_base_url: str
    llm_auth_header: str
    llm_rpm: int
    llm_daily_cap: int
    search_provider: str
    tavily_api_key: str
    serper_api_key: str
    search_monthly_cap: int
    max_pages_per_person: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            contact_email=os.getenv("CONTACT_EMAIL", "").strip(),
            window_years=_int("QS_WINDOW_YEARS", 10),
            llm_provider=os.getenv("LLM_PROVIDER", "none").strip().lower(),
            # LLM_API_KEY / LLM_MODEL work for every provider; the GEMINI_* names still work for older .env files.
            gemini_api_key=(os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip(),
            gemini_model=(os.getenv("LLM_MODEL") or os.getenv("GEMINI_MODEL") or "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_auth_header=os.getenv("LLM_AUTH_HEADER", "").strip(),
            llm_rpm=max(1, _int("LLM_RPM", 8)),
            llm_daily_cap=_int("LLM_DAILY_CAP", 1200),
            search_provider=os.getenv("SEARCH_PROVIDER", "none").strip().lower(),
            tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
            serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
            search_monthly_cap=_int("SEARCH_MONTHLY_CAP", 900),
            max_pages_per_person=_int("MAX_PAGES_PER_PERSON", 5),
        )

    @property
    def llm_api_key(self) -> str:
        return self.gemini_api_key

    @property
    def llm_model(self) -> str:
        return self.gemini_model

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engine.sqlite3"

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def since_year(self) -> int:
        return dt.date.today().year - self.window_years

    @property
    def user_agent(self) -> str:
        contact = self.contact_email or "contact-not-set"
        return f"WBSAlumniImpactResearch/0.1 (Warwick Business School research; {contact})"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.pages_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)
