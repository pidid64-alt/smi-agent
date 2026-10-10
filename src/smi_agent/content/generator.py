"""Генерация оригинального текста (ТЗ §20–23): LLM по базе фактов со строгой проверкой либо честная эвристика-заготовка."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.text import clip, detect_language, sentences
from ..ingestion.features import extract_numbers
from ..llm.service import isolate
from .factbase import FactBase
from .originality import copy_report
from .platforms import I18N, LIMITS, WHY_LIBRARY

log = logging.getLogger(__name__)
_QUOTE_RX = re.compile(r"«([^»]{8,})»|“([^”]{8,})”|\"([^\"]{8,})\"")
_URL_RX = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class CoreDraft:
    headline: str
    lead: str
    points: list[str]
    context: str
    why_it_matters: str
    language: str
    generator: str
    used_fact_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attributions: list[str] = field(default_factory=list)

    def all_text(self) -> str:
        return "\n".join([self.headline, self.lead, *self.points, self.context, self.why_it_matters])


@dataclass
class Brief:
    language: str
    tone: str = "neutral"
    angle: str = ""
    emphasis: str = ""
    length: str = ""  # shorter | longer | ""
    length_multiplier: float = 1.0
    format_code: str = "post_card"
    platforms: list[str] = field(default_factory=lambda: ["telegram", "instagram", "facebook"])
    max_hashtags: dict[str, int] = field(default_factory=dict)
    default_hashtags: list[str] = field(default_factory=list)
    forbidden_words: list[str] = field(default_factory=list)
    signature: str = ""
    political: bool = False
    copy_max_run_words: int = 10
    copy_max_ratio: float = 0.12
    category_label: str = ""


# ------------------------------------------------------------------ защита от галлюцинаций
def _num_key_set(fb: FactBase) -> set[tuple[str, float]]:
    out: set[tuple[str, float]] = set()
    for n in fb.all_numbers:
        out.add((n["unit"], float(n["value"])))
    for f in fb.facts:
        for n in f.numbers:
            out.add((n["u"], float(n["v"])))
    return out


def validate_generated(text: str, fb: FactBase, language: str, know: Any) -> list[str]:
    """Возвращает список нарушений. Пусто — текст можно использовать."""
    problems: list[str] = []
    known = _num_key_set(fb)
    for n in extract_numbers(text, language):
        v, u = float(n["v"]), n["u"]
        if not any(abs(v - kv) <= max(1e-9, 0.01 * abs(kv)) and (u == ku or not u or not ku) for ku, kv in known):
            problems.append(f"число «{n['raw']}» отсутствует в базе фактов")
    allowed_quotes = [re.sub(r"[^\w\s]", "", q.text.lower()) for q in fb.quotes]
    for m in _QUOTE_RX.finditer(text):
        raw_q = next(g for g in m.groups() if g)
        if len(raw_q.split()) < 4:  # «Алтын-Финанс», «Сарыарка» — названия, а не цитаты (как и при извлечении цитат из источников)
            continue
        q = re.sub(r"[^\w\s]", "", raw_q.lower())
        if not any(q in aq or aq in q for aq in allowed_quotes):
            problems.append("в тексте есть цитата, которой нет в базе фактов")
    allowed_urls = {s["url"] for s in fb.sources}
    for u in _URL_RX.findall(text):
        if u.rstrip(".,)") not in allowed_urls:
            problems.append("в тексте есть ссылка, которой нет среди источников")
    if len(text) > 200 and detect_language(text) != language:
        problems.append(f"язык текста не совпадает с запрошенным ({language})")
    if know.count("political_agitation", text):
        problems.append("обнаружены агитационные формулировки")
    return problems


# ------------------------------------------------------------------- эвристическая заготовка
_SPLIT_CLAUSE = re.compile(r"\s*(?:[,;:]|\s[—–-]\s|\sчто\s|\sа также\s|\sоднако\s|\sпри этом\s|\sкоторы[йеая]\s)\s*", re.IGNORECASE)


def _window(words: list[str], center: int, size: int) -> list[str]:
    start = max(0, min(center - size // 2, len(words) - size))
    return words[start : start + size]


def fragment(sentence: str, max_words: int = 9) -> str:
    """Сжимает факт до короткого фрагмента с ключевыми данными (число/сущность), не превышающего max_words слов подряд."""
    s = re.sub(r"\s+", " ", sentence).strip().rstrip(".!?…")
    words = s.split()
    if len(words) <= max_words:
        return s
    clauses = [c for c in _SPLIT_CLAUSE.split(s) if len(c.split()) >= 3]
    scored = []
    for c in clauses or [s]:
        sc = (2 if extract_numbers(c) else 0) + (1 if re.search(r"[A-ZА-ЯЁ]", c[1:]) else 0) + min(len(c.split()), 9) / 9
        scored.append((sc, c))
    best = max(scored)[1]
    bw = best.split()
    if len(bw) > max_words:
        num = extract_numbers(best)
        idx = next((i for i, w in enumerate(bw) if num and num[0]["raw"].split()[0] in w), len(bw) // 2)
        bw = _window(bw, idx, max_words)
    return " ".join(bw)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


class ContentGenerator:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---------------------------------------------------------------- ядро текста
    def core(self, fb: FactBase, brief: Brief, *, project_id: int | None = None) -> CoreDraft:
        notes: list[str] = []
        llm = self.ctx.llm
        if llm.enabled:
            draft = self._core_llm(fb, brief, project_id, notes)
            if draft is not None:
                return draft
            notes.append("LLM недоступна или ответ отклонён проверками — использована эвристическая заготовка")
        draft = self._core_heuristic(fb, brief)
        draft.notes = notes + draft.notes
        return draft

    def _core_llm(self, fb: FactBase, brief: Brief, project_id: int | None, notes: list[str]) -> CoreDraft | None:
        words = int({"shorter": 90, "longer": 220}.get(brief.length, 150) * brief.length_multiplier)
        user = (
            f"Язык текста: {brief.language}. Тон: {brief.tone}. Угол: {brief.angle or 'нейтральное изложение фактов'}. "
            f"Акцент: {brief.emphasis or 'нет'}. Объём основного текста: до {words} слов.\n"
            "Правила: 1) используй только факты из fact_base, числа — в точности; 2) факты с attributed_to сопровождай словами «по данным …»; "
            "3) не пиши оценок и прогнозов от лица источников; 4) переформулируй своими словами, не копируй фразы; "
            + ("5) тема политическая: нейтральный тон, без агитации, оценок и прогнозов, спорные утверждения атрибутируй; " if brief.political else "")
            + "6) в блоке unknowns — то, что пока неизвестно: не выдавай это за факт.\n"
            f"{isolate(json.dumps(fb.for_prompt(), ensure_ascii=False), 14000)}\n"
            'Верни JSON: {"headline": str, "lead": str, "points": [str, ...3-5], "context": str, "why_it_matters": str, "used_fact_ids": [int]}'
        )
        data = self.ctx.llm.run_json("core_draft", user, required={"headline": str, "lead": str, "points": list}, project_id=project_id, max_tokens=1400)
        if data is None:
            return None
        points = [str(p).strip() for p in data["points"] if str(p).strip()][:6]
        draft = CoreDraft(
            headline=clip(str(data["headline"]).strip(), 140), lead=str(data["lead"]).strip(), points=points, context=str(data.get("context", "") or "").strip(),
            why_it_matters=str(data.get("why_it_matters", "") or "").strip(), language=brief.language, generator=f"llm:{self.ctx.llm.model_name}",
            used_fact_ids=[int(i) for i in data.get("used_fact_ids", []) if isinstance(i, int)], attributions=[f.attributed_to for f in fb.facts if f.attributed_to],
        )  # fmt: skip
        problems = validate_generated(draft.all_text(), fb, brief.language, self.ctx.know)
        if len(points) < 2:
            problems.append("слишком мало пунктов")
        rep = copy_report(draft.all_text(), fb.source_texts)
        if rep.max_run_words >= brief.copy_max_run_words or rep.overlap_ratio > brief.copy_max_ratio:
            problems.append(f"слишком близко к источнику (общая фраза {rep.max_run_words} слов, совпадение {rep.overlap_ratio:.0%})")
        if problems:
            notes.append("Ответ LLM отклонён: " + "; ".join(problems[:3]))
            return None
        return draft

    def _core_heuristic(self, fb: FactBase, brief: Brief) -> CoreDraft:
        """Заготовка для редактора без LLM: целые проверенные предложения на нужном языке с атрибуцией.

        Это НЕ оригинальный текст: он совпадает с источниками и не пройдёт проверку оригинальности — редактор должен переписать его
        своими словами (или подключите LLM). Так система не превращается в рерайтер/автопостер (ТЗ §62).
        """
        lang = brief.language
        labels = I18N.get(lang, I18N["ru"])
        notes = [
            "Черновик-заготовка собран без LLM из проверенных фактов (целые предложения с атрибуцией). Перепишите своими словами — "
            "иначе проверка оригинальности не пройдёт; автопубликация запрещена."
        ]
        same_lang = [f for f in fb.facts if detect_language(f.text) == lang]
        facts = (same_lang or fb.facts)[: 3 if brief.length == "shorter" else int(5 * max(0.6, brief.length_multiplier))]
        if same_lang == [] and fb.facts:
            notes.append(f"Факты найдены на другом языке, чем запрошено («{lang}»): автоматический перевод без LLM недоступен.")
        src_names = {s["key"]: s["name"] for s in fb.sources}
        points: list[str] = []
        used: list[int] = []
        for f in facts:
            text = clip(re.sub(r"\s+", " ", f.text).strip(), 240, "")
            if f.attributed_to and f.sources and labels["source_line"] not in text.lower():
                text = text.rstrip(".") + f" ({labels['source_line']} {src_names.get(f.sources[0], f.sources[0])})."
            points.append(text)
            used.append(f.id)
        sub = next((st for st in fb.subtopics if st in WHY_LIBRARY), None) or (fb.category if fb.category in WHY_LIBRARY else None)
        why = WHY_LIBRARY[sub].get(lang, "") if sub else ""
        return CoreDraft(headline=self._pick_headline(fb, lang), lead="", points=points, context="", why_it_matters=why, language=lang, generator="heuristic", used_fact_ids=used, notes=notes)

    def _pick_headline(self, fb: FactBase, lang: str) -> str:
        best = re.sub(r"\s+", " ", fb.title).strip().rstrip(".")
        return clip(best, 120)

    @staticmethod
    def _fmt_date(d: str) -> str:
        parts = d.split("-")
        if len(parts) == 3:
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
        if len(parts) == 2:
            return f"{parts[1]}.{parts[0]}"
        return d

    # ------------------------------------------------------------- адаптация под платформу
    def adapt(self, platform: str, core: CoreDraft, fb: FactBase, brief: Brief, *, reviewed: bool = False) -> dict[str, Any]:
        """Возвращает {format, title, body, hashtags, extras}. Тексты платформ пишутся отдельно — не копии друг друга."""
        from .adapters import adapt_heuristic

        if self.ctx.llm.enabled and not core.generator.startswith("heuristic"):
            out = self._adapt_llm(platform, core, fb, brief)
            if out is not None:
                return out
        return adapt_heuristic(platform, core, fb, brief)

    def _adapt_llm(self, platform: str, core: CoreDraft, fb: FactBase, brief: Brief) -> dict[str, Any] | None:
        lim = LIMITS[platform]
        payload = json.dumps({"core": {"headline": core.headline, "lead": core.lead, "points": core.points, "context": core.context, "why_it_matters": core.why_it_matters}, "fact_base": fb.for_prompt()}, ensure_ascii=False)
        spec = {
            "telegram": "Пост для Telegram-канала: заголовок жирным (<b>), абзацы, 3–5 коротких пунктов, строка источников; до 1800 знаков; 0–3 хэштега; допустимы теги <b>, <i>.",
            "instagram": "Подпись к посту Instagram: сильная первая строка (до 125 знаков), короткие абзацы, 3–8 хэштегов; до 1500 знаков; без ссылок. Верни также slides — список из 4–8 коротких текстов карусели (до 140 знаков каждый) и reels — {hook, beats:[...3-5], cta}.",
            "facebook": "Пост для Facebook: короткий заголовок, 2–3 абзаца с контекстом, прямые ссылки на источники допустимы; до 1500 знаков; 0–3 хэштега.",
        }[platform]
        user = (
            f"Язык: {brief.language}. Тон: {brief.tone}. Платформа: {platform}. {spec}\n"
            "Пиши специально под платформу (не копируй текст других платформ), только факты из fact_base/core, числа — точно, оригинальными формулировками.\n"
            f"{isolate(payload, 14000)}\n"
            'Верни JSON: {"title": str, "body": str, "hashtags": [str], "slides": [str], "reels": {"hook": str, "beats": [str], "cta": str}}'
        )
        data = self.ctx.llm.run_json(f"adapt_{platform}", user, required={"body": str}, max_tokens=1300)
        if data is None:
            return None
        body = str(data["body"]).strip()
        problems = validate_generated(body, fb, brief.language, self.ctx.know)
        rep = copy_report(body, fb.source_texts)
        if rep.max_run_words >= brief.copy_max_run_words or rep.overlap_ratio > brief.copy_max_ratio:
            problems.append("слишком близко к источнику")
        limit = lim.get("text") or lim.get("caption") or 2200
        if len(body) > limit:
            problems.append("превышена длина")
        if problems:
            log.info("adapt %s rejected: %s", platform, problems[:2])
            return None
        tags = [t if t.startswith("#") else "#" + re.sub(r"\W+", "", t) for t in data.get("hashtags", []) if isinstance(t, str) and t.strip()]
        extras: dict[str, Any] = {}
        if platform == "instagram":
            extras = {"slides": [str(x)[:140] for x in data.get("slides", []) if isinstance(x, str)][:8], "reels": data.get("reels") if isinstance(data.get("reels"), dict) else {}}
        fmt = {"telegram": "post", "facebook": "post", "instagram": "carousel" if extras.get("slides") else "photo"}[platform]
        return {"format": fmt, "title": str(data.get("title", core.headline))[:200], "body": body, "hashtags": tags[: brief.max_hashtags.get(platform, 3)], "extras": extras, "generator": core.generator}

    # экспорт для тестов
    @staticmethod
    def escape(text: str) -> str:
        return html.escape(text, quote=False)


__all__ = ["ContentGenerator", "CoreDraft", "Brief", "validate_generated", "fragment", "sentences"]
