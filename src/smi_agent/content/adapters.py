"""Нативная адаптация под платформы (ТЗ §23): Telegram, Instagram, Facebook — разные структура, длина, форматирование."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from ..core.text import clip
from .platforms import I18N, LIMITS

if TYPE_CHECKING:
    from .factbase import FactBase
    from .generator import Brief, CoreDraft

_TAG_CLEAN = re.compile(r"[^\w]", re.UNICODE)


def make_hashtags(fb: FactBase, brief: Brief, platform: str, category_label: str) -> list[str]:
    limit = brief.max_hashtags.get(platform, LIMITS[platform].get("hashtags_soft", LIMITS[platform]["hashtags"]))
    tags: list[str] = list(brief.default_hashtags)
    if fb.geo in ("kz", "ca"):
        tags.append("Казахстан" if brief.language != "en" else "Kazakhstan")
    tags.append(category_label.split(" и ")[0])
    for ent in fb.entities:
        w = ent.split()[0] if ent else ""
        if len(w) >= 4 and not w.isdigit():
            tags.append(w)
    seen: set[str] = set()
    out = []
    for t in tags:
        t = "#" + _TAG_CLEAN.sub("", t.replace(" ", ""))
        stem = t.lower()[:7]  # «Казахстан»/«Казахстана» — один тег
        if len(t) > 2 and stem not in seen:
            seen.add(stem)
            out.append(t)
    return out[:limit]


def _src_names(fb: FactBase, *, independent_first: bool = True, limit: int = 4) -> list[dict[str, Any]]:
    seen, out = set(), []
    ordered = sorted(fb.sources, key=lambda s: (not s["independent"], not s["official"]))
    for s in ordered:
        if s["key"] in seen:
            continue
        seen.add(s["key"])
        out.append(s)
    return out[:limit]


def _tg_html(core: CoreDraft, fb: FactBase, brief: Brief, labels: dict[str, str]) -> str:
    e = lambda t: html.escape(t, quote=False)  # noqa: E731
    parts = [f"<b>{e(core.headline)}</b>"]
    if core.lead and core.lead not in core.points[:1]:
        parts.append(e(core.lead))
    if core.points:
        parts.append("\n".join("• " + e(p) for p in core.points))
    if core.context:
        parts.append(e(core.context))
    if core.why_it_matters:
        parts.append(f"<i>{labels['why']}:</i> {e(core.why_it_matters)}")
    links = []
    for s in _src_names(fb):
        if s["url"].startswith(("http://", "https://")):
            links.append(f'<a href="{html.escape(s["url"], quote=True)}">{e(s["name"])}</a>')
        else:
            links.append(e(s["name"]))
    if links:
        parts.append(f"{labels['sources']}: " + ", ".join(links))
    return "\n\n".join(parts)


def _ig_caption(core: CoreDraft, fb: FactBase, brief: Brief, labels: dict[str, str]) -> str:
    first = clip(core.headline, 120)
    parts = [first]
    if core.points:
        parts.append("\n".join("— " + p for p in core.points[:4]))
    if core.why_it_matters:
        parts.append(core.why_it_matters)
    names = ", ".join(s["name"] for s in _src_names(fb, limit=3))
    if names:
        parts.append(f"{labels['sources']}: {names}")  # в подписях IG ссылки не кликабельны — только названия
    return "\n\n".join(parts)


def _fb_post(core: CoreDraft, fb: FactBase, brief: Brief, labels: dict[str, str]) -> str:
    parts = [core.headline]
    if core.lead:
        parts.append(core.lead)
    if core.points[1:]:
        parts.append("\n".join("• " + p for p in core.points[1:5]))
    if core.context:
        parts.append(core.context)
    if core.why_it_matters:
        parts.append(f"{labels['why']}: {core.why_it_matters}")
    src = [f"{s['name']}: {s['url']}" for s in _src_names(fb, limit=3) if s["url"].startswith("http")]
    if src:
        parts.append(f"{labels['sources']}:\n" + "\n".join(src))
    return "\n\n".join(parts)


def adapt_heuristic(platform: str, core: CoreDraft, fb: FactBase, brief: Brief) -> dict[str, Any]:
    labels = I18N.get(core.language, I18N["ru"])
    cat_label = brief.category_label or fb.category
    tags = make_hashtags(fb, brief, platform, cat_label)
    if platform == "telegram":
        body = _tg_html(core, fb, brief, labels)
        return {"format": "post", "title": core.headline, "body": body, "hashtags": tags, "extras": {"parse_mode": "HTML", "caption_variant": clip_html_caption(core, fb, labels)}, "generator": core.generator}
    if platform == "instagram":
        slides = [clip(core.headline, 90), *[clip(p, 140) for p in core.points[:6]]]
        names = ", ".join(s["name"] for s in _src_names(fb, limit=3))
        slides.append(f"{labels['sources']}: {names}")
        slides = slides[:10]
        reels = {
            "hook": clip(core.headline, 70), "beats": [clip(p, 90) for p in core.points[:4]],
            "cta": clip(f"{labels['sources']}: {names}", 90), "est_seconds": min(35, 6 + 6 * len(core.points[:4])),
        }  # fmt: skip
        fmt = {"carousel_numbers": "carousel", "reels_script": "reels"}.get(brief.format_code, "photo")
        return {"format": fmt, "title": core.headline, "body": _ig_caption(core, fb, brief, labels), "hashtags": tags, "extras": {"slides": slides, "reels": reels}, "generator": core.generator}
    if platform == "facebook":
        return {"format": "post", "title": core.headline, "body": _fb_post(core, fb, brief, labels), "hashtags": tags[:3], "extras": {}, "generator": core.generator}
    raise ValueError(f"Неизвестная платформа: {platform}")


def clip_html_caption(core: CoreDraft, fb: FactBase, labels: dict[str, str], limit: int = 1000) -> str:
    """Короткий вариант для подписи к фото в Telegram (лимит 1024 знака)."""
    e = lambda t: html.escape(t, quote=False)  # noqa: E731
    out = f"<b>{e(core.headline)}</b>"
    for p in core.points[:3]:
        nxt = out + "\n• " + e(p)
        if len(nxt) > limit - 80:
            break
        out = nxt
    names = ", ".join(e(s["name"]) for s in _src_names(fb, limit=3))
    tail = f"\n\n{labels['sources']}: {names}" if names else ""
    return out + tail
