"""Зависимости API: контейнер, аутентификация (cookie-сессия или сервисный токен), CSRF, права в проекте, ограничитель частоты."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import Depends, Request

from ..container import Container
from ..core.errors import AppError, Forbidden, Unauthorized
from ..security.auth import Principal
from ..security.rbac import Actor, Perm, authorize

COOKIE = "smi_session"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class TooManyRequests(AppError):
    status = 429
    code = "rate_limited"


def get_ctx(request: Request) -> Container:
    return request.app.state.ctx


def client_ip(request: Request) -> str:
    # за обратным прокси доверяем X-Forwarded-For только если это явно включено (SMI_TRUST_PROXY=1 в окружении прокси-конфига)
    if getattr(request.app.state, "trust_proxy", False):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _origin_ok(request: Request, ctx: Container) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # не браузер (curl/скрипты): защищает CSRF-токен
    o = urlsplit(origin)
    allowed = {urlsplit(ctx.settings.public_url).netloc, request.headers.get("host", "")}
    return o.netloc in allowed


def get_principal(request: Request, ctx: Container = Depends(get_ctx)) -> Principal:
    ip = client_ip(request)
    auth = request.headers.get("authorization", "")
    principal: Principal | None = None
    if auth.lower().startswith("bearer "):
        principal = ctx.auth.authenticate_token(auth[7:].strip(), ip=ip)
    else:
        token = request.cookies.get(COOKIE, "")
        principal = ctx.auth.authenticate_session(token, ip=ip) if token else None
        if principal is not None and request.method in UNSAFE:
            if not _origin_ok(request, ctx) or not ctx.auth.csrf_ok(principal.csrf, request.headers.get("x-csrf-token")):
                raise Forbidden("Запрос отклонён защитой от CSRF", code="csrf")
    if principal is None:
        raise Unauthorized("Требуется вход", code="unauthenticated")
    if not principal.mfa_ok and not request.url.path.startswith("/api/auth/"):
        raise Forbidden("Для администратора обязательна двухфакторная аутентификация: включите MFA в профиле", code="mfa_setup_required")
    request.state.principal = principal
    return principal


def project_actor(project_id: int, request: Request, principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)) -> Actor:
    with ctx.db.read() as s:
        return ctx.auth.actor_for(s, principal, project_id)


def require(actor: Actor, perm: Perm) -> None:
    authorize(actor, perm)


class RateLimiter:
    """Скользящее окно в памяти процесса: защита от всплесков (для распределённых установок — на уровне прокси)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: int) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                raise TooManyRequests("Слишком много запросов. Повторите позже", code="rate_limited", details={"retry_after_s": window_s})
            q.append(now)


def limit(name: str, per: int, window_s: int = 60) -> Callable[..., None]:
    def dep(request: Request) -> None:
        rl: RateLimiter = request.app.state.limiter
        who = getattr(getattr(request, "state", None), "principal", None)
        key = f"{name}:{who.user_id if who else client_ip(request)}"
        rl.check(key, per, window_s)

    return dep
