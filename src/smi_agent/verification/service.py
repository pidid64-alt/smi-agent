"""Проверка фактов и подтверждений (ТЗ §14, §61.8): первоисточник, независимые подтверждения, цифры/даты/цитаты, противоречия."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import VERIFICATION_LABELS, VerificationStatus
from ..core.text import sentences
from ..db.models import Article, Event, Verification
from ..ingestion.features import extract_numbers

_DOMAIN_IN_TEXT = re.compile(r"\b((?:[a-z0-9\-]+\.)+(?:kz|gov|int|org|ru|com|uk|eu|cn))\b", re.IGNORECASE)


@dataclass
class VerificationResult:
    status: VerificationStatus
    score: float
    primary: dict[str, Any]
    n_independent: int
    checks: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    interpretations: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _check(key: str, label: str, status: str, detail: str) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "detail": detail}


class VerificationService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def evaluate(self, s: Session, ev: Event) -> VerificationResult:
        know = self.ctx.know
        arts = list(s.scalars(select(Article).where(Article.event_id == ev.id).order_by(Article.published_at)))
        flags = ev.flags or {}
        direct = [a for a in arts if a.independent]
        n_ind = ev.n_independent
        checks: list[dict[str, Any]] = []
        warnings: list[str] = []

        # 1. первичный источник
        official = [a for a in arts if a.source.is_official or know.is_official_url(a.url)]
        mentioned_domains = set()
        for a in arts:
            for dom in _DOMAIN_IN_TEXT.findall(f"{a.summary} {a.body[:2500]}"):
                if know.is_official_url("https://" + dom.lower()):
                    mentioned_domains.add(dom.lower())
        cited_official = sum(1 for a in direct if (a.features or {}).get("flags", {}).get("official_markers"))
        top_single = len(direct) == 1 and direct[0].source.reliability >= 0.9
        if official:
            primary = {"found": True, "kind": "official", "name": official[0].source.name, "url": official[0].url}
            checks.append(_check("primary_source", "Первоисточник", "pass", f"Официальный источник: {official[0].source.name}"))
        elif mentioned_domains:
            d = sorted(mentioned_domains)[0]
            primary = {"found": True, "kind": "official_linked", "name": d, "url": f"https://{d}"}
            checks.append(_check("primary_source", "Первоисточник", "pass", f"В материалах упомянут официальный ресурс: {d} (документ не загружался — откройте и сверьте)"))
            warnings.append(f"Первоисточник ({d}) упомянут в текстах, но не подгружен автоматически")
        elif cited_official >= 2:
            primary = {"found": False, "kind": "cited_official", "name": "", "url": ""}
            checks.append(_check("primary_source", "Первоисточник", "warn", "Несколько материалов ссылаются на официальное заявление, но сам документ не найден"))
            warnings.append("Официальное заявление упоминается, но первоисточник не найден — найдите и проверьте его")
        else:
            primary = {"found": False, "kind": "none", "name": "", "url": ""}
            checks.append(_check("primary_source", "Первоисточник", "warn", "Первичный (официальный) источник не найден среди материалов"))

        # 2. независимые подтверждения
        cited_only = flags.get("cited_only") or (ev.features or {}).get("cited_only_origins", [])
        if n_ind >= 3:
            checks.append(_check("independent", "Независимые подтверждения", "pass", f"{n_ind} независимых источника(ов)"))
        elif n_ind == 2:
            checks.append(_check("independent", "Независимые подтверждения", "pass", "2 независимых источника"))
        else:
            checks.append(_check("independent", "Независимые подтверждения", "warn", "Только один независимый источник" + (f"; остальные ссылаются на {', '.join(cited_only)}" if cited_only else "")))
        if ev.n_articles > ev.n_independent:
            warnings.append(f"Из {ev.n_articles} публикаций независимых лишь {ev.n_independent}: остальные — перепечатки и обновления")

        # 3. цифры: противоречия в заголовках независимых материалов и числа, встречающиеся лишь у одного источника
        contradictions: list[dict[str, Any]] = []
        by_unit: dict[str, list[tuple[float, str, str]]] = {}
        for a in direct:
            for n in extract_numbers(a.title, a.lang):
                if n["u"]:
                    by_unit.setdefault(n["u"], []).append((float(n["v"]), a.source.name, n["raw"]))
        for unit, vals in by_unit.items():
            for i in range(len(vals)):
                for j in range(i + 1, len(vals)):
                    (v1, s1, r1), (v2, s2, r2) = vals[i], vals[j]
                    if s1 != s2 and abs(v1 - v2) / max(abs(v1), abs(v2), 1e-9) > 0.02:
                        contradictions.append({"type": "numbers", "detail": f"В заголовках разные значения: {r1} ({s1}) и {r2} ({s2})"})
        feat_nums = (ev.features or {}).get("numbers", [])
        multi_nums = [n for n in feat_nums if len(n.get("sources", [])) >= 2]
        single_nums = [n for n in feat_nums if len(n.get("sources", [])) == 1 and n.get("u")]
        if contradictions:
            checks.append(_check("numbers", "Цифры", "fail", contradictions[0]["detail"]))
        elif feat_nums and n_ind >= 2 and multi_nums:
            checks.append(_check("numbers", "Цифры", "pass", f"{len(multi_nums)} значени(я/й) совпадает у нескольких источников"))
        elif feat_nums:
            checks.append(_check("numbers", "Цифры", "warn", "Цифры приводит один источник — сверьте с первоисточником"))
        else:
            checks.append(_check("numbers", "Цифры", "pass", "В материалах нет ключевых цифр"))
        if single_nums and n_ind >= 3:
            warnings.append(f"Значение {single_nums[0]['raw']} встречается только в одном источнике")

        # 4. даты
        dates = (ev.features or {}).get("dates", [])
        checks.append(_check("dates", "Даты", "pass", ", ".join(dates[:4]) if dates else "Явные даты в материалах не указаны"))

        # 5. цитаты
        quotes = (ev.features or {}).get("quotes", [])
        if quotes:
            multi_q = [q for q in quotes if len(q.get("sources", [])) >= 2]
            if multi_q:
                checks.append(_check("quotes", "Цитаты", "pass", "Цитата встречается у нескольких источников"))
            else:
                checks.append(_check("quotes", "Цитаты", "warn", "Цитаты найдены у одного источника — сверьте с оригиналом; в тексте атрибутируйте автору"))
        else:
            checks.append(_check("quotes", "Цитаты", "pass", "Прямых цитат нет"))

        # 6. оговорки/слухи и факт против интерпретации
        hedge = float(flags.get("hedge_share", 0))
        opinion = float(flags.get("opinion_share", 0))
        if hedge >= 0.5:
            checks.append(_check("hedging", "Слухи и оговорки", "fail", "Большинство материалов построено на «по неподтверждённым данным/якобы»"))
        elif hedge > 0:
            checks.append(_check("hedging", "Слухи и оговорки", "warn", "Часть материалов использует оговорки о непроверенности"))
        else:
            checks.append(_check("hedging", "Слухи и оговорки", "pass", "Оговорок о непроверенности нет"))
        facts_out, interp_out = self._facts_vs_interpretation(ev, arts, direct)
        if opinion >= 0.5:
            checks.append(_check("interpretation", "Факты и интерпретации", "warn", "Преобладают мнения и оценки; в материале разделяйте факты и мнения"))
        else:
            checks.append(_check("interpretation", "Факты и интерпретации", "pass", f"Фактов: {len(facts_out)}, интерпретаций: {len(interp_out)}"))

        # 7. надёжность источников и безопасность текста
        avg_rel = sum(a.source.reliability for a in direct) / len(direct) if direct else 0.0
        checks.append(_check("reliability", "Надёжность источников", "pass" if avg_rel >= 0.7 else "warn", f"средняя оценка {avg_rel:.2f}"))
        if flags.get("injection"):
            checks.append(_check("injection", "Безопасность текста", "fail", "В источнике обнаружены инструкции для ИИ (prompt-injection) — текст изолирован"))
            warnings.append("В тексте источника найдены подозрительные инструкции; автоматические правки отключены")
        if ev.flags.get("paywalled_only"):
            warnings.append("Доступны только заголовки/анонсы платных источников — факты нужно проверить по оригиналу")

        # решение
        failed = [c for c in checks if c["status"] == "fail"]
        if contradictions and n_ind < 2:
            status = VerificationStatus.REJECTED
        elif hedge >= 0.6 and n_ind < 2 and not primary["found"]:
            status = VerificationStatus.REJECTED
        elif failed and any(c["key"] in ("numbers", "hedging") for c in failed):
            status = VerificationStatus.NEEDS_CHECK
        elif n_ind >= 2 and (primary["found"] or avg_rel >= 0.6):
            status = VerificationStatus.MULTI_CONFIRMED
        elif primary["found"] and primary["kind"] in ("official", "official_linked"):
            status = VerificationStatus.CONFIRMED
        elif top_single and not hedge and len(facts_out) >= 1:
            status = VerificationStatus.CONFIRMED
            warnings.append(f"Подтверждено одним надёжным источником ({direct[0].source.name}); независимого подтверждения пока нет")
        else:
            status = VerificationStatus.NEEDS_CHECK
        score = (
            0.35 * min(1.0, n_ind / 3) + 0.25 * (1.0 if primary["found"] else 0.0) + 0.15 * (0.0 if contradictions else 1.0) + 0.10 * (1 - hedge)
            + 0.10 * avg_rel + 0.05 * (1.0 if not quotes or any(len(q.get("sources", [])) >= 2 for q in quotes) else 0.5)
        )  # fmt: skip
        if status == VerificationStatus.REJECTED:
            score *= 0.4
        return VerificationResult(status, round(max(0.0, min(1.0, score)), 3), primary, n_ind, checks, facts_out, interp_out, contradictions, warnings)

    def _facts_vs_interpretation(self, ev: Event, arts: list[Article], direct: list[Article]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        know = self.ctx.know
        facts: list[dict[str, Any]] = []
        for f in (ev.features or {}).get("facts", [])[:6]:
            txt = f["text"]
            nums = {f"{n['u']}:{float(n['v']):.3g}" for n in extract_numbers(txt)}
            support = 0
            for a in direct:
                a_nums = {f"{n['u']}:{float(n['v']):.3g}" for n in (a.features or {}).get("numbers", [])}
                if nums and nums <= a_nums:
                    support += 1
                elif not nums and txt[:40].lower() in (a.body or "").lower():
                    support += 1
            facts.append({"text": txt, "source": f.get("source"), "support": max(support, 1)})
        interp: list[dict[str, Any]] = []
        for a in sorted(arts, key=lambda x: -x.source.reliability)[:3]:
            for sent in sentences(a.body or a.summary)[:14]:
                if know.count("opinion", sent) + know.count("hedges", sent) and 25 <= len(sent) <= 260:
                    interp.append({"text": sent, "source": a.source.key})
                    break
        return facts, interp[:4]

    def verify_event(self, s: Session, ev: Event, run_id: int | None = None) -> Verification:
        res = self.evaluate(s, ev)
        row = Verification(
            project_id=ev.project_id, event_id=ev.id, run_id=run_id, status=res.status.value, score=res.score, primary_source=res.primary,
            n_independent=res.n_independent, checks=res.checks, facts=res.facts, interpretations=res.interpretations, contradictions=res.contradictions,
            warnings=res.warnings, verified_at=self.ctx.clock.now(),
        )  # fmt: skip
        s.add(row)
        ev.verification_status = res.status.value
        s.flush()
        return row

    @staticmethod
    def label(status: str) -> str:
        try:
            return VERIFICATION_LABELS[VerificationStatus(status)]
        except (ValueError, KeyError):
            return status
