"""Ошибки публикации и классификация исхода попытки: от неё зависит, можно ли повторять отправку (идемпотентность, ТЗ §34–35)."""

from __future__ import annotations

import httpx

from ..core.enums import AttemptOutcome
from ..security.redaction import redact


class PlatformError(Exception):
    """Ошибка платформы с известной классификацией исхода."""

    def __init__(self, outcome: AttemptOutcome, code: str, message: str, *, http_status: int | None = None, retry_after: float | None = None, reauth: bool = False, excerpt: str = ""):
        super().__init__(message)
        self.outcome = outcome
        self.code = code
        self.message = redact(message)[:600]
        self.http_status = http_status
        self.retry_after = retry_after
        self.reauth = reauth  # токен недействителен/отозван — аккаунту нужна повторная авторизация
        self.excerpt = redact(excerpt)[:600]


def classify_exception(e: Exception) -> PlatformError:
    """Сетевые исключения: «гарантированно не отправлено» только если соединение даже не было установлено."""
    if isinstance(e, PlatformError):
        return e
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return PlatformError(AttemptOutcome.NOT_SENT, "connect_failed", f"Не удалось подключиться к платформе ({type(e).__name__})")
    if isinstance(e, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.TimeoutException)):
        return PlatformError(AttemptOutcome.UNKNOWN, "no_response", f"Ответ платформы не получен ({type(e).__name__}): возможно, публикация выполнена")
    return PlatformError(AttemptOutcome.UNKNOWN, "unexpected", f"Непредвиденная ошибка при отправке ({type(e).__name__})")


def classify_http(status: int, *, body_excerpt: str = "", retry_after: float | None = None, platform_code: str = "") -> PlatformError:
    """Ответ получен ⇒ известно, что платформа сказала. 4xx (кроме 429/408) — публикации нет; 5xx — исход неизвестен."""
    ex = body_excerpt[:400]
    if status == 429:
        return PlatformError(AttemptOutcome.RATE_LIMITED, platform_code or "rate_limited", "Превышен лимит запросов платформы", http_status=status, retry_after=retry_after or 60, excerpt=ex)
    if status in (401, 403):
        return PlatformError(AttemptOutcome.FAILED_PERMANENT, platform_code or "auth", "Доступ запрещён: токен недействителен или нет прав", http_status=status, reauth=True, excerpt=ex)
    if 400 <= status < 500 and status != 408:
        return PlatformError(AttemptOutcome.FAILED_PERMANENT, platform_code or f"http_{status}", f"Платформа отклонила запрос (HTTP {status})", http_status=status, excerpt=ex)
    if status == 408 or status >= 500:
        return PlatformError(AttemptOutcome.UNKNOWN, platform_code or f"http_{status}", f"Сбой на стороне платформы (HTTP {status}): исход публикации неизвестен", http_status=status, excerpt=ex)
    return PlatformError(AttemptOutcome.UNKNOWN, f"http_{status}", f"Неожиданный ответ HTTP {status}", http_status=status, excerpt=ex)
