"""Карточка предложения (ТЗ §16): Название / Почему сейчас / Что произошло / Почему интересно / Источники / Проверка / Угол / Формат."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.clock import ensure_utc
from ..content.factbase import typo_quotes
from ..core.enums import RELATION_LABELS, VERIFICATION_LABELS, VerificationStatus
from ..core.text import detect_language
from ..db.models import Article, Event, Verification

CATEGORY_ANGLES = {
    "economy": "Цифры и контекст: что изменилось, кого это касается и что известно о следующих шагах — без прогнозов",
    "finance": "Цифры и контекст: что изменилось для рынка и для обычных людей — без инвестиционных советов",
    "business": "Что сделала компания, какие цифры и факты подтверждены и почему это заметно для рынка Казахстана",
    "energy": "Что изменилось на рынке топлива и энергии и как это может отразиться на ценах и тарифах",
    "ai": "Что нового на самом деле, как это повлияет на обычных пользователей и что пока остаётся неизвестным",
    "tech": "Что нового, как это работает простыми словами и что это значит для пользователей в Казахстане",
    "auto": "Что это значит для автовладельцев и покупателей: цены, сроки, условия",
    "transport": "Что меняется для пассажиров и жителей: маршруты, сроки, стоимость проезда",
    "science": "Главное открытие простыми словами: что нашли, как проверяли и почему это важно",
    "unusual": "Необычный факт, рассказанный коротко и точно, с понятным контекстом",
    "health": "Что сообщают официальные источники и что это значит для людей; без медицинских советов",
    "education": "Что меняется для школьников, студентов и родителей: сроки, правила, последствия",
    "realty": "Что меняется для жильцов, покупателей и арендаторов: цены, правила, сроки",
    "law": "Что именно решил суд или изменилось в законе, кого это касается и с какой даты",
    "politics": "Нейтральное изложение фактов: кто что заявил и решил, со ссылками на источники, без оценок",
    "incident": "Что известно на данный момент из проверенных источников, без спекуляций и подробностей, вредящих пострадавшим",
    "international": "Почему это важно и как связано с Казахстаном и Центральной Азией (только при наличии подтверждённой связи)",
    "society": "Что происходит, кого касается и что можно сделать людям",
    "sport": "Главное о событии: результат, цифры и контекст",
    "culture": "Главное о событии и почему о нём говорят",
}
DEFAULT_ANGLE = "Главное за 30 секунд: что произошло, почему это важно и что известно дальше"
PRACTICAL_SUBTOPICS = {"tariffs_prices": "цены и тарифы", "banking": "кредиты и банки", "housing": "жильё", "education_exams": "образование", "healthcare": "здравоохранение", "currency": "курс валют"}


def _ago(now: datetime, t: datetime) -> str:
    hours = max(0.0, (now - ensure_utc(t)).total_seconds() / 3600)
    if hours < 1:
        return f"{max(1, int(hours * 60))} мин назад"
    if hours < 36:
        return f"{hours:.0f} ч назад"
    return f"{hours / 24:.0f} дн назад"


def why_now(ev: Event, now: datetime) -> str:
    c = ev.components or {}
    parts: list[str] = []
    ex = c.get("explain", {}).get("velocity", "")
    if "быстро набирает" in ex:
        parts.append("Тема быстро набирает популярность: " + ex.split(" — ")[0])
    age = (now - ensure_utc(ev.first_published_at)).total_seconds() / 3600
    if age < 3:
        parts.append(f"Новость появилась {_ago(now, ev.first_published_at)}")
    if (ev.flags or {}).get("official_present"):
        parts.append("есть официальное сообщение")
    upcoming = sorted(d for d in (ev.features or {}).get("dates", []) if len(d) == 5 and d > now.strftime("%m-%d"))
    if upcoming:
        d = upcoming[0]
        parts.append(f"ближайшая дата — {d[3:]}.{d[:2]}")
    rk = (ev.flags or {}).get("repeat_kind")
    if rk == "new_stage":
        parts.append("это новая стадия уже знакомой аудитории истории")
    if not parts:
        parts.append(f"Последнее обновление {_ago(now, ev.last_update_at)}, независимых источников: {ev.n_independent}")
    text = "; ".join(parts)
    return text[0].upper() + text[1:].rstrip(".") + "."


def why_interesting(ev: Event, know: Any) -> str:
    v = (ev.components or {}).get("values", {})
    out: list[str] = []
    pr = v.get("practical_value", 0)
    if pr >= 0.55:
        topics = [PRACTICAL_SUBTOPICS[t] for t in (ev.subtopics or []) if t in PRACTICAL_SUBTOPICS]
        out.append("Затрагивает повседневную жизнь" + (f" ({', '.join(topics[:2])})" if topics else ""))
    if v.get("kz_significance", 0) >= 0.6 and ev.geo_bucket == "KZ":
        out.append("важно для Казахстана" if not out else "значимо для Казахстана")
    elif v.get("world_significance", 0) >= 0.6:
        out.append("заметное событие мировой повестки")
    if v.get("discussion", 0) >= 0.45:
        out.append("тема вызывает обсуждение и споры")
    if v.get("audience_interest", 0.5) >= 0.65:
        out.append("совпадает с интересами вашей аудитории по прошлым выборам")
    if v.get("novelty", 0) >= 0.7 and not out:
        out.append("новая тема, которой ещё не было в вашей ленте")
    if ev.category == "unusual" and not out:
        out.append("необычный факт — хорошо подходит для короткого формата")
    if not out:
        out.append("достаточно подтверждённых фактов для оригинального материала")
    s = ", ".join(out)
    return s[0].upper() + s[1:] + "."


def suggest_angle(ev: Event) -> str:
    base = CATEGORY_ANGLES.get(ev.category, DEFAULT_ANGLE)
    if ev.geo_bucket == "WORLD" and ev.category not in ("international", "science", "unusual") and (ev.kz_relevance or 0) > 0.15:
        base += "; отдельно — что известно о влиянии на Казахстан"
    return base


def suggest_format(ev: Event) -> dict[str, Any]:
    nums = len((ev.features or {}).get("numbers", []))
    if ev.category in ("unusual", "science", "sport", "culture") and nums < 3:
        code, label = "reels_script", "Короткий пост для Telegram/Facebook и сценарий Reels (30–40 сек) + обложка"
        plat = {"telegram": "post", "facebook": "post", "instagram": "reels"}
    elif nums >= 3:
        code, label = "carousel_numbers", "Пост в Telegram/Facebook и карусель Instagram с карточками цифр"
        plat = {"telegram": "post", "facebook": "post", "instagram": "carousel"}
    elif ev.category in ("politics", "law", "incident", "health"):
        code, label = "short_post", "Короткий нейтральный пост со ссылками на источники + карточка с главным фактом"
        plat = {"telegram": "post", "facebook": "post", "instagram": "photo"}
    else:
        code, label = "post_card", "Пост для Telegram/Facebook + фото-карточка для Instagram"
        plat = {"telegram": "post", "facebook": "post", "instagram": "photo"}
    return {"code": code, "label": label, "platforms": plat}


def build_card(s: Session, ev: Event, ver: Verification | None, now: datetime, know: Any, *, slot: int = 0) -> dict[str, Any]:
    arts = list(s.scalars(select(Article).where(Article.event_id == ev.id).order_by(Article.published_at)))
    sources = []
    for a in arts[:10]:
        sources.append({
            "name": a.source.name, "key": a.source.key, "url": a.url, "tier": a.source.tier, "country": a.source.country,
            "published_at": ensure_utc(a.published_at).isoformat(), "relation": a.relation, "relation_label": RELATION_LABELS.get(a.relation, a.relation),
            "independent": a.independent, "official": bool(a.source.is_official),
        })  # fmt: skip
    all_facts = [f["text"] for f in (ver.facts if ver and ver.facts else (ev.features or {}).get("facts", []))]
    if len(all_facts) < 2:
        all_facts = [f["text"] for f in (ev.features or {}).get("facts", [])]
    same_lang = [t for t in all_facts if detect_language(t) == "ru"]  # карточка — на языке интерфейса, если фактов на нём достаточно
    facts = [typo_quotes(t) for t in (same_lang if len(same_lang) >= 2 else all_facts)][:4]
    status = ver.status if ver else (ev.verification_status or VerificationStatus.NEEDS_CHECK.value)
    try:
        label = VERIFICATION_LABELS[VerificationStatus(status)]
    except ValueError:
        label = status
    fmt = suggest_format(ev)
    return {
        "slot": slot,
        "event_id": ev.id,
        "title": ev.title,
        "why_now": why_now(ev, now),
        "what_happened": facts,
        "why_interesting": why_interesting(ev, know),
        "sources": sources,
        "verification": {
            "status": status, "label": label, "score": ver.score if ver else None, "warnings": (ver.warnings if ver else [])[:4],
            "checks": [{"key": c["key"], "label": c["label"], "status": c["status"]} for c in (ver.checks if ver else [])],
            "primary": (ver.primary_source if ver else {}) or {},
        },
        "angle": suggest_angle(ev),
        "format": fmt,
        "category": ev.category,
        "category_label": know.category_label(ev.category),
        "geo": ev.geo,
        "geo_bucket": ev.geo_bucket,
        "trend_score": ev.trend_score,
        "phase": ev.phase,
        "n_independent": ev.n_independent,
        "n_articles": ev.n_articles,
        "sensitive": bool((ev.flags or {}).get("sensitive") or (ev.flags or {}).get("sensitive_category")),
        "political": bool((ev.flags or {}).get("political")),
        "repeat": {"kind": (ev.flags or {}).get("repeat_kind"), "of": (ev.flags or {}).get("repeat_of")} if (ev.flags or {}).get("repeat_kind") else None,
    }  # fmt: skip
