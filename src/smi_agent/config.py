"""Конфигурация процесса (переменные окружения SMI_*). Бизнес-настройки проекта хранятся в БД (см. settings_model)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMI_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "production"] = "dev"
    demo_mode: bool = False
    database_url: str = "sqlite:///./data/smi_agent.db"
    data_dir: Path = Path("./data")
    config_dir: Path = Path("./config")
    public_url: str = "http://localhost:8000"
    # Мастер-ключи шифрования секретов: "kid1:<base64url 32 bytes>,kid0:<...>"; первый — текущий.
    master_keys: SecretStr = SecretStr("")
    backup_key: SecretStr = SecretStr("")  # отдельный ключ для шифрования резервных копий (base64url 32 bytes)
    session_idle_minutes: int = 12 * 60
    session_absolute_hours: int = 24 * 7
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    require_mfa_for_admin: bool | None = None  # None → True в production
    enable_docs: bool | None = None  # None → только вне production
    embedded_worker: bool = False  # запускать фоновые задачи внутри процесса API (удобно для демо)
    # Исходящие запросы (SSRF / контроль внешних обращений)
    user_agent: str = "SmiAgentBot/0.1 (+https://github.com/pidid64-alt/smi-agent)"
    fetch_timeout_s: float = 15.0
    fetch_max_bytes: int = 4_000_000
    fetch_max_redirects: int = 3
    fetch_allowed_ports: str = "80,443"
    allow_private_fetch: bool = False  # только для тестов/локальной отладки
    respect_robots: bool = True
    # LLM (опционально; без него работают эвристики)
    llm_provider: Literal["none", "openai_compat", "anthropic"] = "none"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_timeout_s: float = 60.0
    llm_daily_call_budget: int = 2000
    embedding_provider: Literal["hashing", "openai_compat"] = "hashing"
    embedding_model: str = ""
    # Платформы
    meta_graph_version: str = "v26.0"
    meta_app_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    telegram_api_base: str = "https://api.telegram.org"
    graph_api_base: str = "https://graph.facebook.com"
    media_public_base: str = ""  # базовый публичный URL для медиа (по умолчанию public_url)
    # Резервное копирование / RPO-RTO
    backup_dir: Path = Path("./backups")
    backup_interval_min: int = 15
    backup_retention: int = 96

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def mfa_required_for_admin(self) -> bool:
        return self.is_production if self.require_mfa_for_admin is None else self.require_mfa_for_admin

    @property
    def docs_enabled(self) -> bool:
        return (not self.is_production) if self.enable_docs is None else self.enable_docs

    @property
    def allowed_ports(self) -> set[int]:
        return {int(p) for p in self.fetch_allowed_ports.split(",") if p.strip()}

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def media_base_url(self) -> str:
        return (self.media_public_base or self.public_url).rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
