"""Ограничения и особенности платформ (ТЗ §26–29). Проверены по документации на 2026-09; значения — в одном месте."""

from __future__ import annotations

from typing import Any

LIMITS: dict[str, dict[str, Any]] = {
    "telegram": {"text": 4096, "caption": 1024, "hashtags": 3, "parse_mode": "HTML", "formats": ["post", "photo", "gallery"]},
    "instagram": {"caption": 2200, "hashtags": 30, "hashtags_soft": 8, "carousel_max": 10, "reels_max_s": 90, "first_line": 125, "formats": ["photo", "carousel", "reels", "stories"], "image_format": "JPEG"},
    "facebook": {"text": 63206, "text_soft": 2200, "hashtags": 3, "formats": ["post", "photo", "link"]},
}

I18N: dict[str, dict[str, str]] = {
    "ru": {"facts": "Что известно", "why": "Почему это важно", "sources": "Источники", "context": "Контекст", "unknown": "Пока неизвестно", "figures": "Цифры", "source_line": "по данным"},
    "kk": {"facts": "Белгілі жайттар", "why": "Неге маңызды", "sources": "Дереккөздер", "context": "Контекст", "unknown": "Әзірге белгісіз", "figures": "Сандар", "source_line": "дерек бойынша"},
    "en": {"facts": "What we know", "why": "Why it matters", "sources": "Sources", "context": "Context", "unknown": "Not yet known", "figures": "Figures", "source_line": "according to"},
}

# Общие определения/пояснения (не утверждения о конкретном событии), ключ — подтема или категория.
WHY_LIBRARY: dict[str, dict[str, str]] = {
    "banking": {"ru": "Базовая ставка — ориентир для стоимости кредитов и депозитов в банках.", "en": "The base rate is a benchmark for loan and deposit costs at banks."},
    "currency": {"ru": "Курс валют влияет на цены импортных товаров и на сбережения в разных валютах.", "en": "Exchange rates affect the prices of imported goods and savings held in different currencies."},
    "tariffs_prices": {"ru": "Изменение цен и тарифов напрямую отражается на семейном бюджете.", "en": "Changes in prices and tariffs directly affect household budgets."},
    "housing": {"ru": "Решения в сфере жилья влияют на стоимость покупки, аренды и обслуживания квартир.", "en": "Housing decisions affect the cost of buying, renting and maintaining homes."},
    "electric_vehicles": {"ru": "Развитие электромобилей и зарядной инфраструктуры влияет на выбор автомобиля и расходы владельцев.", "en": "EV and charging infrastructure developments influence car choices and owners' costs."},
    "generative_ai": {"ru": "Новые модели ИИ меняют инструменты, которыми пользуются люди и компании.", "en": "New AI models change the tools that people and companies use."},
    "cybersecurity": {"ru": "Утечки и атаки касаются любого, кто пользуется онлайн-сервисами.", "en": "Leaks and attacks concern anyone who uses online services."},
    "space": {"ru": "Космические исследования расширяют научные знания и технологические возможности.", "en": "Space research expands scientific knowledge and technological capabilities."},
    "oil_gas": {"ru": "Рынок нефти и газа важен для экономики Казахстана и цен на топливо.", "en": "Oil and gas markets matter for Kazakhstan's economy and fuel prices."},
    "education_exams": {"ru": "Решения в образовании затрагивают школьников, студентов и их семьи.", "en": "Education decisions affect students and their families."},
    "healthcare": {"ru": "Новости здравоохранения касаются здоровья и доступности медицинской помощи.", "en": "Healthcare news concerns health and access to medical care."},
    "economy": {"ru": "Экономические показатели отражаются на доходах, ценах и занятости.", "en": "Economic indicators affect incomes, prices and employment."},
    "transport": {"ru": "Изменения в транспорте влияют на ежедневные поездки жителей.", "en": "Transport changes affect residents' daily commutes."},
}
