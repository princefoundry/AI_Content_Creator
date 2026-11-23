"""Central configuration for the AEVRIX Content Intelligence Engine.

All settings are loaded from environment variables / a local ``.env`` file.
No secrets are hardcoded. Each integration is optional and the application
degrades gracefully when credentials are absent.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"


class LLMProvider(StrEnum):
    FOUNDRY = "foundry"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    GEMINI = "gemini"
    GROQ = "groq"


class TelegramMode(StrEnum):
    POLLING = "polling"
    WEBHOOK = "webhook"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application ----
    app_env: str = "development"
    app_timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"
    dashboard_password: str = "change-me"

    # ---- Database ----
    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR / 'aevrix.db'}"

    # ---- LLM provider ----
    llm_provider: LLMProvider = LLMProvider.OLLAMA
    llm_model: str = ""

    foundry_project_endpoint: str = ""
    foundry_model_deployment: str = ""
    foundry_api_key: str = ""
    foundry_api_version: str = "2024-05-01-preview"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    openai_compatible_base_url: str = ""
    openai_compatible_api_key: str = ""
    openai_compatible_model: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"

    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"

    # ---- Telegram ----
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_webhook_secret: str = ""
    telegram_mode: TelegramMode = TelegramMode.POLLING

    # ---- X API ----
    x_client_id: str = ""
    x_client_secret: str = ""
    x_redirect_uri: str = "http://localhost:8000/auth/x/callback"
    x_scopes: str = "tweet.read tweet.write users.read offline.access"

    # ---- LinkedIn API ----
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_redirect_uri: str = "http://localhost:8000/auth/linkedin/callback"
    linkedin_scopes: str = "openid profile w_member_social"
    # LinkedIn requires a YYYYMM version header; old versions are retired after
    # ~2 years and return HTTP 426 "version not active". Keep this current.
    linkedin_api_version: str = "202610"

    # ---- Search ----
    search_provider: str = ""
    search_api_key: str = ""
    # NewsData.io request mix. The hard cap protects API credits even if the
    # per-market counts or optional query strings are configured too high.
    newsdata_india_queries_per_run: int = 4
    newsdata_us_queries_per_run: int = 2
    newsdata_global_queries_per_run: int = 2
    newsdata_max_requests_per_run: int = 8
    newsdata_results_per_request: int = 10
    newsdata_priority_domain: str = ""
    newsdata_remove_duplicates: bool = True
    # Optional pipe-separated queries appended to each market's built-in pool.
    newsdata_india_extra_queries: str = ""
    newsdata_us_extra_queries: str = ""
    newsdata_global_extra_queries: str = ""

    # ---- Security ----
    app_encryption_key: str = ""
    app_session_secret: str = "change-me-to-a-long-random-string"

    # ---- Limits ----
    max_articles_per_run: int = 60
    max_llm_calls_per_run: int = 50
    max_x_posts_per_day: int = 5
    max_linkedin_posts_per_day: int = 4
    max_linkedin_posts_per_week: int = 4
    research_window_hours: int = 24
    # Pending/needs-revision drafts are auto-deleted after this many days so the
    # review queue never piles up stale cards.
    draft_retention_days: int = 7
    require_human_approval: bool = True
    auto_publish: bool = False

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _strip_ids(cls, v: str) -> str:
        return (v or "").strip()

    # ---- Derived helpers ----
    @property
    def allowed_telegram_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.telegram_allowed_user_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids

    @property
    def resolved_model(self) -> str:
        """The model name to use for the active provider."""
        if self.llm_model:
            return self.llm_model
        return {
            LLMProvider.FOUNDRY: self.foundry_model_deployment,
            LLMProvider.OLLAMA: self.ollama_model,
            LLMProvider.OPENAI_COMPATIBLE: self.openai_compatible_model,
            LLMProvider.GEMINI: self.gemini_model,
            LLMProvider.GROQ: self.groq_model,
        }.get(self.llm_provider, "")

    def feature_enabled(self, feature: str) -> bool:
        """Return True when the named integration has the config it needs."""
        checks = {
            "telegram": bool(self.telegram_bot_token and self.allowed_telegram_ids),
            "x": bool(self.x_client_id and self.x_client_secret),
            "linkedin": bool(self.linkedin_client_id and self.linkedin_client_secret),
            "search": bool(self.search_provider and self.search_api_key),
            "encryption": bool(self.app_encryption_key),
        }
        return checks.get(feature, False)

    def provider_configured(self, provider: LLMProvider | None = None) -> bool:
        """Return True if the given (or active) provider has required config."""
        p = provider or self.llm_provider
        if p == LLMProvider.FOUNDRY:
            return bool(self.foundry_project_endpoint and self.foundry_api_key)
        if p == LLMProvider.OLLAMA:
            return bool(self.ollama_base_url)
        if p == LLMProvider.OPENAI_COMPATIBLE:
            return bool(self.openai_compatible_base_url)
        if p == LLMProvider.GEMINI:
            return bool(self.gemini_api_key)
        if p == LLMProvider.GROQ:
            return bool(self.groq_api_key)
        return False

    @property
    def sync_database_url(self) -> str:
        """Synchronous SQLite URL (used by APScheduler / LangGraph checkpointer)."""
        return self.database_url.replace("+aiosqlite", "")

    @property
    def sqlite_path(self) -> str:
        """Filesystem path to the SQLite database file."""
        url = self.database_url
        if ":///" in url:
            return url.split(":///", 1)[1]
        return str(DATA_DIR / "aevrix.db")


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
