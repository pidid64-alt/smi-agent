"""Клиенты LLM (OpenAI-совместимый и Anthropic). Подключаются настройками; без них система работает на эвристиках."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from ..security.redaction import redact


class LlmError(Exception):
    pass


@dataclass
class LlmResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class LlmClient(Protocol):
    name: str
    model: str

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.4, json_mode: bool = False) -> LlmResult: ...


class OpenAICompatClient:
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        if not model:
            raise LlmError("Не задана модель (SMI_LLM_MODEL)")
        self.base_url, self.model = base_url.rstrip("/"), model
        self._key = api_key
        self._client = httpx.Client(timeout=timeout, transport=transport, trust_env=False)

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.4, json_mode: bool = False) -> LlmResult:
        body: dict = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        try:
            r = self._client.post(f"{self.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {self._key}"} if self._key else {})
        except httpx.HTTPError as e:
            raise LlmError(f"сеть: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise LlmError(f"HTTP {r.status_code}: {redact(r.text[:200])}")
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage", {})
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LlmError("некорректный ответ провайдера") from e
        return LlmResult(text, data.get("model", self.model), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), int((time.monotonic() - t0) * 1000))


class AnthropicClient:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com", timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        if not model:
            raise LlmError("Не задана модель (SMI_LLM_MODEL)")
        self.model, self.base_url = model, base_url.rstrip("/")
        self._key = api_key
        self._client = httpx.Client(timeout=timeout, transport=transport, trust_env=False)

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.4, json_mode: bool = False) -> LlmResult:
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system, "messages": [{"role": "user", "content": user}]}
        t0 = time.monotonic()
        try:
            r = self._client.post(f"{self.base_url}/v1/messages", json=body, headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"})
        except httpx.HTTPError as e:
            raise LlmError(f"сеть: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise LlmError(f"HTTP {r.status_code}: {redact(r.text[:200])}")
        try:
            data = r.json()
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            usage = data.get("usage", {})
        except (ValueError, KeyError, TypeError) as e:
            raise LlmError("некорректный ответ провайдера") from e
        return LlmResult(text, data.get("model", self.model), usage.get("input_tokens", 0), usage.get("output_tokens", 0), int((time.monotonic() - t0) * 1000))


def build_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> LlmClient | None:
    if settings.llm_provider == "none":
        return None
    key = settings.llm_api_key.get_secret_value()
    if settings.llm_provider == "openai_compat":
        host = urlsplit(settings.llm_base_url).hostname or ""
        if settings.is_production and settings.llm_base_url.startswith("http://") and host not in ("localhost", "127.0.0.1"):
            raise LlmError("В production для LLM требуется https")
        return OpenAICompatClient(settings.llm_base_url, key, settings.llm_model, settings.llm_timeout_s, transport)
    if settings.llm_provider == "anthropic":
        base = settings.llm_base_url if "anthropic" in settings.llm_base_url else "https://api.anthropic.com"
        return AnthropicClient(key, settings.llm_model, base, settings.llm_timeout_s, transport)
    return None
