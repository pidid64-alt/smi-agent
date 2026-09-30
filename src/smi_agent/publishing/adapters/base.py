"""Контракт адаптеров платформ и безопасный HTTP-клиент для официальных API (только API, без эмуляции браузера — ТЗ §24)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from ...config import Settings
from ..errors import PlatformError, classify_exception, classify_http

OFFICIAL_HOSTS = {"api.telegram.org", "graph.facebook.com", "graph.instagram.com", "rupload.facebook.com"}


@dataclass
class MediaFile:
    asset_id: int
    role: str
    path: Path
    mime: str
    public_url: str
    alt_text: str = ""


@dataclass
class PublishContext:
    publication_id: int
    platform: str
    fmt: str
    text: str
    parse_mode: str | None
    media: list[MediaFile]
    account_external_id: str
    account_handle: str
    token: str  # расшифрован непосредственно перед вызовом; не логируется, не сохраняется
    progress: dict[str, Any]  # сохранённый прогресс многошаговой публикации (контейнеры IG и т.п.)
    idempotency_key: str
    save_progress: Any = None  # callable(dict) — сохраняет прогресс между шагами
    extras: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)  # настройки аккаунта (sandbox и т.д.)
    claimed_at: Any = None


@dataclass
class PublishResult:
    external_id: str
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcileResult:
    status: str  # found | not_found | unknown
    external_id: str = ""
    url: str = ""
    detail: str = ""


@dataclass
class AccountCheck:
    ok: bool
    display_name: str = ""
    external_id: str = ""
    handle: str = ""
    detail: str = ""
    scopes: list[str] = field(default_factory=list)
    reauth: bool = False


class PublisherAdapter(Protocol):
    platform: str

    def publish(self, ctx: PublishContext) -> PublishResult: ...
    def reconcile(self, ctx: PublishContext) -> ReconcileResult: ...
    def check_account(self, external_id: str, token: str) -> AccountCheck: ...
    def fetch_metrics(self, external_id: str, token: str, *, fmt: str = "post") -> dict[str, Any]: ...


class PlatformHttp:
    """HTTP к официальным API: фиксированные хосты, без редиректов, таймауты, токен — только в заголовке (не в URL и не в логах)."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.client = httpx.Client(timeout=httpx.Timeout(settings.fetch_timeout_s * 2, connect=10.0), follow_redirects=False, trust_env=False, transport=transport)
        self._transport_overridden = transport is not None

    def _check_host(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host in OFFICIAL_HOSTS or self._transport_overridden or (self.settings.env != "production" and self.settings.allow_private_fetch):
            return
        raise PlatformError(classify_http(400).outcome, "host_not_allowed", f"Хост {host} не входит в список официальных API")

    def request(self, method: str, url: str, *, token: str = "", params: dict[str, Any] | None = None, data: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> httpx.Response:
        self._check_host(url)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            return self.client.request(method, url, params=params, data=data, json=json_body, files=files, headers=headers)
        except httpx.HTTPError as e:
            raise classify_exception(e) from e

    def close(self) -> None:
        self.client.close()
