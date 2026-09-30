"""Безопасный HTTP-клиент для сбора данных: SSRF-защита, лимиты, редиректы, robots.txt, вежливые паузы."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..config import Settings
from ..core.errors import FetchError, SSRFBlocked
from ..security.ssrf import EgressGuard, GuardedTransport, Resolver, UrlPolicy

DEFAULT_ACCEPT = ("text/", "application/xml", "application/rss+xml", "application/atom+xml", "application/json", "application/xhtml+xml", "application/feed+json")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    redirects: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def text(self) -> str:
        enc = "utf-8"
        ct = self.headers.get("content-type", "")
        if "charset=" in ct:
            enc = ct.split("charset=")[-1].split(";")[0].strip().strip('"') or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class SafeHttp:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        allowed_hosts: set[str] | None = None,
        min_interval_s: float = 0.0,
    ):
        self.settings = settings
        policy = UrlPolicy(allowed_ports=settings.allowed_ports, allow_private=settings.allow_private_fetch, allowed_hosts=allowed_hosts)
        self.guard = EgressGuard(policy, resolver)
        self._transport = transport or GuardedTransport(self.guard)
        self._client = httpx.Client(
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,  # прокси из окружения обходили бы защиту подключения
            headers={"User-Agent": settings.user_agent, "Accept-Language": "ru,kk;q=0.9,en;q=0.8"},
        )
        self.min_interval_s = min_interval_s
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ helpers
    def _polite_wait(self, host: str) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            wait = self._last_hit.get(host, 0.0) + self.min_interval_s - time.monotonic()
            self._last_hit[host] = time.monotonic() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def robots_allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is None or time.monotonic() - cached[0] > 6 * 3600:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                r = self.get(origin + "/robots.txt", max_bytes=500_000, accept=("text/",), check_robots=False, timeout=10)
                if r.status in (401, 403) or r.status >= 500:
                    parser = None  # RFC 9309: недоступен → считаем запрещённым
                elif r.status >= 400:
                    parser.parse([])
                else:
                    parser.parse(r.text().splitlines())
            except FetchError:
                parser = None
            self._robots[origin] = (time.monotonic(), parser)
            cached = self._robots[origin]
        parser = cached[1]
        if parser is None:
            return False
        return parser.can_fetch(self.settings.user_agent, url)

    # --------------------------------------------------------------------- GET
    def get(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        accept: tuple[str, ...] | None = DEFAULT_ACCEPT,
        headers: dict[str, str] | None = None,
        check_robots: bool = False,
        timeout: float | None = None,
    ) -> FetchResult:
        max_bytes = max_bytes or self.settings.fetch_max_bytes
        timeout = timeout or self.settings.fetch_timeout_s
        deadline = time.monotonic() + timeout * 2
        redirects: list[str] = []
        current = url
        started = time.monotonic()
        for _hop in range(self.settings.fetch_max_redirects + 1):
            self.guard.check_url(current)  # повторная проверка на каждом хопе
            if check_robots and not self.robots_allowed(current):
                raise FetchError("Доступ запрещён robots.txt")
            host = urlsplit(current).hostname or ""
            self._polite_wait(host)
            try:
                with self._client.stream("GET", current, headers=headers, timeout=httpx.Timeout(timeout, connect=min(10.0, timeout))) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        nxt = urljoin(current, resp.headers["location"])
                        redirects.append(nxt)
                        current = nxt
                        continue
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if resp.status_code == 200 and accept and ctype and not any(ctype.startswith(a) for a in accept):
                        raise FetchError(f"Недопустимый тип содержимого: {ctype}")
                    clen = resp.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        raise FetchError("Ответ слишком большой")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError("Ответ слишком большой")
                        if time.monotonic() > deadline:
                            raise FetchError("Превышено время ожидания ответа")
                        chunks.append(chunk)
                    return FetchResult(
                        url=url, final_url=current, status=resp.status_code,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        content=b"".join(chunks), redirects=redirects,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
            except (FetchError, SSRFBlocked):
                raise
            except httpx.TimeoutException as e:
                raise FetchError("Тайм-аут запроса") from e
            except httpx.HTTPError as e:
                raise FetchError(f"Ошибка сети: {type(e).__name__}") from e
        raise FetchError("Слишком много перенаправлений")
