"""Защита от SSRF и контроль исходящих запросов (ТЗ §36).

Слои: (1) политика URL (схема/порт/userinfo/хост), (2) резолв DNS и запрет непубличных адресов,
(3) закрепление IP на уровне TCP-подключения (защита от DNS-rebinding), (4) ручная обработка редиректов
с повторной проверкой каждого хопа, (5) лимиты размера/времени ответа.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpcore
import httpx

from ..core.errors import SSRFBlocked

Resolver = Callable[[str, int], list[str]]

_BLOCKED_NAMES = {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata", "instance-data"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa", ".intranet", ".corp")


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            return ip_is_public(str(ip.sixtofour))
        elif ip in ipaddress.ip_network("64:ff9b::/96"):
            return ip_is_public(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return bool(ip.is_global) and not ip.is_multicast


@dataclass
class UrlPolicy:
    allowed_ports: set[int] = field(default_factory=lambda: {80, 443})
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    allow_private: bool = False
    allowed_hosts: set[str] | None = None  # None — любой публичный хост; иначе точное совпадение или суффикс

    def host_allowed(self, host: str) -> bool:
        if self.allowed_hosts is None:
            return True
        h = host.lower().rstrip(".")
        return any(h == a or h.endswith("." + a) for a in self.allowed_hosts)


class EgressGuard:
    def __init__(self, policy: UrlPolicy | None = None, resolver: Resolver | None = None):
        self.policy = policy or UrlPolicy()
        self.resolver = resolver or default_resolver

    def parse(self, url: str) -> tuple[str, str, int, str]:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        if scheme not in self.policy.allowed_schemes:
            raise SSRFBlocked(f"Схема {scheme or '—'} запрещена")
        if parts.username or parts.password:
            raise SSRFBlocked("URL с учётными данными запрещён")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise SSRFBlocked("В URL отсутствует хост")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as e:
            raise SSRFBlocked("Некорректное имя хоста") from e
        port = parts.port or (443 if scheme == "https" else 80)
        if port not in self.policy.allowed_ports:
            raise SSRFBlocked(f"Порт {port} запрещён")
        if not self.policy.host_allowed(host):
            raise SSRFBlocked(f"Хост {host} не входит в список разрешённых")
        return scheme, host, port, parts.path or "/"

    def resolve_public(self, host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
            ips = [host]
        except ValueError:
            if not self.policy.allow_private and (host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES)):
                raise SSRFBlocked(f"Хост {host} запрещён") from None
            try:
                ips = self.resolver(host, port)
            except Exception as e:  # noqa: BLE001 — fail closed: любая ошибка резолва = блокировка
                raise SSRFBlocked(f"Не удалось разрешить имя {host}") from e
        if not ips:
            raise SSRFBlocked(f"Имя {host} не разрешается")
        if not self.policy.allow_private:
            bad = [ip for ip in ips if not ip_is_public(ip)]
            if bad:
                raise SSRFBlocked(f"Адрес {bad[0]} не является публичным")
        return ips

    def precheck(self, url: str) -> None:
        """Проверка без DNS (для сохранения настроек): схема/порт/учётные данные, IP-литералы и запрещённые имена."""
        _, host, _, _ = self.parse(url)
        try:
            ipaddress.ip_address(host)
            if not self.policy.allow_private and not ip_is_public(host):
                raise SSRFBlocked(f"Адрес {host} не является публичным")
        except ValueError:
            if not self.policy.allow_private and (host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES)):
                raise SSRFBlocked(f"Хост {host} запрещён") from None

    def check_url(self, url: str) -> list[str]:
        _, host, port, _ = self.parse(url)
        return self.resolve_public(host, port)


class GuardedBackend(httpcore.NetworkBackend):
    """Сетевой бэкенд httpcore: резолвит имя, проверяет ВСЕ адреса и подключается по проверенному IP."""

    def __init__(self, inner: httpcore.NetworkBackend, guard: EgressGuard):
        self._inner, self._guard = inner, guard

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ANN001
        ips = self._guard.resolve_public(host, port)
        last: Exception | None = None
        for ip in ips:
            try:
                return self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except Exception as e:  # noqa: BLE001
                last = e
        raise last or httpcore.ConnectError("connect failed")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):  # noqa: ANN001
        raise SSRFBlocked("Unix-сокеты запрещены")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class GuardedTransport(httpx.HTTPTransport):
    def __init__(self, guard: EgressGuard, **kwargs):  # noqa: ANN003
        super().__init__(**kwargs)
        pool = self._pool
        if not hasattr(pool, "_network_backend"):  # fail closed: не полагаемся на неизвестную версию httpcore
            raise RuntimeError("Несовместимая версия httpcore: невозможно установить SSRF-защиту")
        pool._network_backend = GuardedBackend(pool._network_backend, guard)
