"""Песочница: «публикует» в никуда. Для демо, разработки и тестов; умеет имитировать сбои (в том числе «ответ потерян после отправки»)."""

from __future__ import annotations

from typing import Any

from ...core.enums import AttemptOutcome
from ..errors import PlatformError
from .base import AccountCheck, PublishContext, PublishResult, ReconcileResult


class SandboxAdapter:
    def __init__(self, platform: str):
        self.platform = platform

    def publish(self, ctx: PublishContext) -> PublishResult:
        mode = (ctx.settings or {}).get("sandbox_failure", "")
        done = ctx.progress.get("sandbox_done")
        if done:
            return PublishResult(done, f"sandbox://{self.platform}/{done}")
        if mode == "connect":
            raise PlatformError(AttemptOutcome.NOT_SENT, "connect_failed", "Песочница: соединение не установлено")
        if mode == "rate_limit":
            raise PlatformError(AttemptOutcome.RATE_LIMITED, "rate_limited", "Песочница: лимит запросов", retry_after=30)
        if mode == "auth":
            raise PlatformError(AttemptOutcome.FAILED_PERMANENT, "auth", "Песочница: токен отозван", reauth=True)
        if mode == "bad_request":
            raise PlatformError(AttemptOutcome.FAILED_PERMANENT, "bad_request", "Песочница: платформа отклонила запрос")
        if mode == "timeout":  # ответ не получен, публикации НЕТ
            raise PlatformError(AttemptOutcome.UNKNOWN, "no_response", "Песочница: таймаут, исход неизвестен")
        ext = f"sbx-{ctx.platform}-{ctx.publication_id}"
        if mode == "timeout_after_send":  # публикация ВЫШЛА, но ответ потерян — проверка сверкой
            if ctx.save_progress:
                ctx.save_progress({**ctx.progress, "sandbox_done": ext})
            raise PlatformError(AttemptOutcome.UNKNOWN, "no_response", "Песочница: таймаут после отправки, исход неизвестен")
        if ctx.save_progress:
            ctx.save_progress({**ctx.progress, "sandbox_done": ext})
        return PublishResult(ext, f"sandbox://{self.platform}/{ext}", {"sandbox": True})

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        done = ctx.progress.get("sandbox_done")
        if done:
            return ReconcileResult("found", done, f"sandbox://{self.platform}/{done}", "найдено в песочнице")
        return ReconcileResult("not_found", detail="В песочнице публикации нет")

    def check_account(self, external_id: str, token: str) -> AccountCheck:
        return AccountCheck(True, display_name=f"Песочница {self.platform}", external_id=external_id or "sandbox", handle="@sandbox", scopes=["sandbox"])

    def fetch_metrics(self, external_id: str, token: str, *, fmt: str = "post") -> dict[str, Any]:
        return {"unavailable": ["views", "reach", "likes", "comments", "shares", "saves"], "note": "Песочница не возвращает реальные метрики"}
