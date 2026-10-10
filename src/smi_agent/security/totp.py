"""TOTP (RFC 6238) для MFA — без внешних зависимостей."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()  # noqa: S324 — требование RFC 6238
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off : off + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp_now(secret: str, *, at: float | None = None, step: int = 30) -> str:
    return _hotp(secret, int((at if at is not None else time.time()) // step))


def verify_totp(secret: str, code: str, *, at: float | None = None, step: int = 30, window: int = 1,
                last_step: int = 0) -> int | None:
    """Возвращает номер шага при успехе (для защиты от повтора) либо None."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return None
    now_step = int((at if at is not None else time.time()) // step)
    for delta in range(-window, window + 1):
        st = now_step + delta
        if st <= last_step:
            continue
        if hmac.compare_digest(_hotp(secret, st), code):
            return st
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Smi-Agent") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
