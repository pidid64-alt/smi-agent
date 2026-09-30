"""Программируемая LLM-заглушка: для тестов и демо-режима (тексты для вымышленных новостей заранее подготовлены).

Проходит через те же проверки, что и настоящая модель: числа из базы фактов, оригинальность, язык, цитаты, ссылки.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .client import LlmError, LlmResult

Responder = Callable[[str, str], "str | dict[str, Any] | None"]


class ScriptedLlm:
    name = "scripted"
    model = "scripted-1"

    def __init__(self, responder: Responder | None = None, *, core: dict[str, Any] | None = None, platforms: dict[str, dict[str, Any]] | None = None, fail: bool = False):
        self.responder, self.core, self.platforms, self.fail = responder, core, platforms or {}, fail
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.4, json_mode: bool = False) -> LlmResult:
        self.calls.append((system, user))
        if self.fail:
            raise LlmError("scripted failure")
        out: Any = None
        if self.responder is not None:
            out = self.responder(system, user)
        if out is None:
            m = re.search(r"Платформа: (\w+)\.", user)
            if m and m.group(1) in self.platforms:
                out = self.platforms[m.group(1)]
            elif '"headline"' in user and self.core is not None and "Платформа:" not in user:
                out = self.core
        if out is None:
            raise LlmError("no scripted answer")
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return LlmResult(text, self.model, len(user) // 4, len(text) // 4, 1)
