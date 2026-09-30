"""Хеширование паролей пользователей системы (scrypt, stdlib). Пароли ВНЕШНИХ платформ не запрашиваются и не хранятся."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_DEFAULT_LOG_N = 15
_COMMON = {
    "password", "password1", "qwerty123", "123456789", "1234567890", "admin", "administrator", "letmein", "welcome1",
    "qwertyuiop", "iloveyou", "changeme", "passw0rd", "12345678", "123456789012",
}  # fmt: skip


def _log_n() -> int:
    return int(os.environ.get("SMI_SCRYPT_LOG_N", _DEFAULT_LOG_N))


def _derive(password: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << log_n, r=r, p=p, maxmem=1 << 28, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    log_n, r, p = _log_n(), 8, 1
    dk = _derive(password, salt, log_n, r, p)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return f"scrypt${log_n}${r}${p}${b64(salt)}${b64(dk)}"


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, log_n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        calc = _derive(password, _unb64(salt), int(log_n), int(r), int(p))
        return hmac.compare_digest(calc, _unb64(dk))
    except Exception:  # noqa: BLE001 — любой мусор в хеше = неверный пароль
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < _log_n()
    except Exception:  # noqa: BLE001
        return True


def check_password_policy(password: str, *, production: bool, username: str = "") -> list[str]:
    problems: list[str] = []
    min_len = 12 if production else 8
    if len(password) < min_len:
        problems.append(f"Пароль должен содержать не менее {min_len} символов")
    if password.lower() in _COMMON:
        problems.append("Слишком простой пароль")
    if len(set(password)) < 5:
        problems.append("Пароль слишком однообразен")
    if username and username.lower() in password.lower():
        problems.append("Пароль не должен содержать имя пользователя")
    return problems


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
