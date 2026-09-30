"""Финальный текст публикации: тело + хэштеги + маркировка участия ИИ. Единый рендер для превью, проверок и публикаторов."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from ..settings_model import ProjectSettings
from .platforms import LIMITS

_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a"}
_TAG_RX = re.compile(r"<(/?)([a-zA-Z]+)([^>]*)>")
_HREF_RX = re.compile(r'^\s+href="(https?://[^"\s<>]+)"\s*$')


def sanitize_tg_html(text: str) -> tuple[str, list[str]]:
    """Оставляет только разрешённые Telegram-теги; прочее экранирует. Возвращает (безопасный HTML, замечания)."""
    issues: list[str] = []
    out: list[str] = []
    pos = 0
    stack: list[str] = []
    for m in _TAG_RX.finditer(text):
        out.append(html.escape(html.unescape(text[pos : m.start()]), quote=False))
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        ok = name in _ALLOWED_TAGS
        if ok and name == "a" and not closing:
            ok = bool(_HREF_RX.match(attrs))
        elif ok and attrs.strip() and not (name == "a" and closing):
            ok = False
        if not ok:
            issues.append(f"недопустимый тег <{name}>")
            out.append(html.escape(m.group(0), quote=False))
        else:
            if closing:
                if stack and stack[-1] == name:
                    stack.pop()
                else:
                    issues.append(f"лишний закрывающий тег </{name}>")
                    out.append(html.escape(m.group(0), quote=False))
                    pos = m.end()
                    continue
            else:
                stack.append(name)
            out.append(m.group(0))
        pos = m.end()
    out.append(html.escape(html.unescape(text[pos:]), quote=False))
    if stack:
        issues.append("не закрыты теги: " + ", ".join(stack))
    return "".join(out), issues


def disclosure_line(cfg: ProjectSettings, lang: str, *, reviewed: bool) -> str:
    c = cfg.content
    if not c.ai_disclosure:
        return ""
    base = c.ai_disclosure_text.get(lang) or c.ai_disclosure_text.get("ru", "")
    if reviewed:
        base += c.ai_disclosure_reviewed_suffix.get(lang, c.ai_disclosure_reviewed_suffix.get("ru", ""))
    return base


@dataclass
class Rendered:
    platform: str
    format: str
    text: str
    parse_mode: str | None
    limit: int
    length: int
    hashtags: list[str]
    disclosure: str
    media_ids: list[int] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def too_long(self) -> bool:
        return self.length > self.limit


def render_final(platform: str, version: Any, cfg: ProjectSettings, *, reviewed: bool = False) -> Rendered:
    lang = version.language or cfg.content.default_language
    disc = disclosure_line(cfg, lang, reviewed=reviewed)
    tags = " ".join(version.hashtags or [])
    media_ids = [m["asset_id"] for m in (version.media or [])]
    extras = version.extras or {}
    lim = LIMITS[platform]
    if platform == "telegram":
        has_photo = version.format in ("photo", "gallery") and bool(media_ids)
        base = extras.get("caption_variant") if has_photo and extras.get("caption_variant") else version.body
        safe, _ = sanitize_tg_html(base)
        parts = [safe]
        if tags:
            parts.append(html.escape(tags, quote=False))
        if disc:
            parts.append(f"<i>{html.escape(disc, quote=False)}</i>")
        text = "\n\n".join(p for p in parts if p)
        plain_len = len(re.sub(r"<[^>]+>", "", text))  # лимит Telegram считается по тексту после разбора разметки
        limit = lim["caption"] if has_photo else lim["text"]
        return Rendered(platform, version.format, text, "HTML", limit, plain_len, list(version.hashtags or []), disc, media_ids, extras)
    parts = [version.body]
    if tags:
        parts.append(tags)
    if disc:
        parts.append(disc)
    text = "\n\n".join(p for p in parts if p)
    limit = lim["caption"] if platform == "instagram" else lim["text"]
    return Rendered(platform, version.format, text, None, limit, len(text), list(version.hashtags or []), disc, media_ids, extras)
