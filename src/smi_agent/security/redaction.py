"""Маскирование секретов в логах, сообщениях и аудите (ТЗ §37: токены не должны попадать в логи)."""

from __future__ import annotations

import logging
import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[telegram-token]"),
    (re.compile(r"\bEA[A-Za-z0-9]{40,}\b"), "[meta-token]"),
    (re.compile(r"\bIG[A-Za-z0-9]{40,}\b"), "[instagram-token]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[api-key]"),
    (re.compile(r"\bsmi_[A-Za-z0-9_-]{20,}\b"), "[smi-token]"),
    (re.compile(r"(?i)((?:access_token|refresh_token|client_secret|api[_-]?key|password|passwd|secret|token|fb_exchange_token)=)[^&\s\"']+"),
     r"\1[redacted]"),
    (re.compile(r"(?i)(\"(?:access_token|refresh_token|client_secret|api[_-]?key|password|secret|token)\"\s*:\s*\")[^\"]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(/bot)\d{6,12}:[A-Za-z0-9_-]{30,}"), r"\1[telegram-token]"),
]
_SENSITIVE_KEY = re.compile(r"(?i)(pass(word|wd)?|secret|token|authorization|cookie|api[_-]?key|credential|csrf|private[_-]?key|ciphertext)")


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    out = str(text)
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def sanitize_details(obj: Any, *, _depth: int = 0, max_str: int = 600) -> Any:
    """Рекурсивно вычищает чувствительные ключи и секреты в строках; ограничивает размер значений."""
    if _depth > 6:
        return "[truncated]"
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if _SENSITIVE_KEY.search(str(k)) and not isinstance(v, bool):
                clean[str(k)] = "[redacted]"
            else:
                clean[str(k)] = sanitize_details(v, _depth=_depth + 1, max_str=max_str)
        return clean
    if isinstance(obj, (list, tuple, set)):
        return [sanitize_details(v, _depth=_depth + 1, max_str=max_str) for v in list(obj)[:50]]
    if isinstance(obj, str):
        r = redact(obj)
        return r if len(r) <= max_str else r[:max_str] + "…"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return redact(str(obj))[:max_str]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(str(record.msg))
            if record.args:
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in (record.args if isinstance(record.args, tuple) else (record.args,)))
        except Exception:  # noqa: BLE001 — фильтр не должен ломать логирование
            pass
        return True


def install_log_redaction() -> None:
    f = RedactingFilter()
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(x, RedactingFilter) for x in h.filters):
            h.addFilter(f)
    # httpx/httpcore логируют URL запросов (у Telegram токен в пути)
    for name in ("httpx", "httpcore", "uvicorn.access"):
        lg = logging.getLogger(name)
        if not any(isinstance(x, RedactingFilter) for x in lg.filters):
            lg.addFilter(f)
