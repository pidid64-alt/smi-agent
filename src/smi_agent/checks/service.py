"""Проверки перед публикацией (ТЗ §32, §61.15): любая проваленная проверка запрещает автопубликацию."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..content.factbase import Fact, FactBase, Quote
from ..content.generator import validate_generated
from ..content.originality import copy_report
from ..content.platforms import LIMITS
from ..content.render import render_final, sanitize_tg_html
from ..core.text import detect_language
from ..db.models import Article, CheckReport, Content, Event, MediaAsset, PlatformVersion, Project
from ..ingestion.features import extract_numbers
from ..security.redaction import redact
from ..settings_model import ProjectSettings, load_project_settings

ALLOWED_RIGHTS = {"own", "generated", "official_free", "licensed"}


def _r(key: str, label: str, status: str, detail: str, blocks: bool = False) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "detail": detail, "blocks_autopilot": blocks or status == "fail"}


def factbase_from_content(content: Content) -> FactBase:
    facts, quotes, nums = [], [], []
    for it in content.fact_base or []:
        k = it.get("kind")
        if k == "fact":
            facts.append(Fact(it["id"], it["text"], it.get("sources", []), it.get("support", 1), it.get("attributed_to"), it.get("numbers", [])))
        elif k == "quote":
            quotes.append(Quote(it["text"], it.get("speaker"), it.get("source", ""), it.get("source_name", "")))
        elif k == "number":
            nums.append({"raw": it["raw"], "value": it["value"], "unit": it["unit"]})
    return FactBase(
        event_id=content.event_id or 0, title=content.title, category=content.category, geo=content.geo, facts=facts, quotes=quotes, dates=[], entities=[],
        sources=list(content.sources or []), unknowns=[], interpretations=[], verification={}, languages=[], sensitive=dict((content.sensitivity or {}).get("topics", {})),
        political=bool((content.sensitivity or {}).get("political")), source_texts={}, all_numbers=nums,
    )  # fmt: skip


class CheckService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def run(self, s: Session, content: Content, version: PlatformVersion, *, reviewed: bool = False) -> CheckReport:
        ctx = self.ctx
        know = ctx.know
        cfg = load_project_settings(s.get(Project, content.project_id).settings)
        platform = version.platform
        rendered = render_final(platform, version, cfg, reviewed=reviewed)
        fb = factbase_from_content(content)
        ev = s.get(Event, content.event_id) if content.event_id else None
        arts = list(s.scalars(select(Article).where(Article.event_id == ev.id))) if ev else []
        source_texts = {f"{a.source.key}#{a.id}": f"{a.title}. {a.body or a.summary}" for a in arts}
        body_only = re.sub(r"<[^>]+>", "", version.body)
        results: list[dict[str, Any]] = []
        add = results.append

        # 1. непустой текст и длина
        if not body_only.strip():
            add(_r("not_empty", "Текст", "fail", "Текст пуст"))
        lim = rendered.limit
        if rendered.too_long:
            add(_r("length", "Длина", "fail", f"{rendered.length} знаков при лимите {lim} для {platform}"))
        else:
            add(_r("length", "Длина", "pass", f"{rendered.length} из {lim} знаков"))
        spec = LIMITS[platform]
        if platform == "instagram":
            first = (version.body.split("\n", 1)[0] or "")[: spec["first_line"] + 50]
            if len(version.body.split("\n", 1)[0]) > spec["first_line"]:
                add(_r("ig_first_line", "Первая строка", "warn", f"Первая строка длиннее {spec['first_line']} знаков и обрежется «…ещё»: {first[:30]}…"))
            if len(version.hashtags or []) > spec["hashtags"]:
                add(_r("hashtags", "Хэштеги", "fail", f"Не более {spec['hashtags']} хэштегов"))
            elif len(version.hashtags or []) > spec["hashtags_soft"]:
                add(_r("hashtags", "Хэштеги", "warn", f"Рекомендуется не более {spec['hashtags_soft']} хэштегов"))
        elif len(version.hashtags or []) > spec.get("hashtags", 3) + 2:
            add(_r("hashtags", "Хэштеги", "warn", "Слишком много хэштегов"))
        if platform == "telegram":
            _safe, issues = sanitize_tg_html(version.body)
            add(_r("html", "Разметка Telegram", "fail" if issues else "pass", "; ".join(issues) if issues else "Разметка корректна"))

        # 2. факты, цифры, цитаты, ссылки
        problems = validate_generated(body_only, fb, version.language, know) if fb.facts or fb.all_numbers else []
        bad_num = [p for p in problems if p.startswith("число")]
        bad_quote = [p for p in problems if "цитата" in p]
        bad_url = [p for p in problems if "ссылка" in p]
        add(_r("numbers", "Цифры соответствуют фактам", "fail" if bad_num else "pass", "; ".join(bad_num[:2]) if bad_num else "Все числа есть в базе фактов"))
        add(_r("quotes", "Цитаты соответствуют источникам", "fail" if bad_quote else "pass", bad_quote[0] if bad_quote else "Посторонних цитат нет"))
        shorteners = know.lex.get("unsafe_url_hints")
        urls = re.findall(r"https?://\S+", version.body)
        if bad_url or (shorteners and any(shorteners.any(u) for u in urls)):
            add(_r("urls", "Ссылки", "fail", "Есть ссылка вне списка источников или сокращатель ссылок"))
        else:
            add(_r("urls", "Ссылки", "pass", "Ссылки только на источники"))

        # 3. оригинальность
        if source_texts:
            rep = copy_report(body_only, source_texts)
            run_lim, ratio_lim = cfg.content.copy_max_run_words, cfg.content.copy_max_ratio
            if rep.max_run_words >= run_lim or rep.overlap_ratio > ratio_lim:
                add(_r("originality", "Оригинальность", "fail", f"Дословная фраза {rep.max_run_words} слов, совпадение {rep.overlap_ratio:.0%} (источник: {rep.worst_source.split('#')[0]})"))
            elif rep.max_run_words >= 0.8 * run_lim or rep.overlap_ratio > 0.8 * ratio_lim:
                add(_r("originality", "Оригинальность", "warn", f"Близко к источнику: фраза {rep.max_run_words} слов, совпадение {rep.overlap_ratio:.0%}", blocks=True))
            else:
                add(_r("originality", "Оригинальность", "pass", f"Совпадение {rep.overlap_ratio:.0%}, макс. общая фраза {rep.max_run_words} сл."))
            norm = lambda t: re.sub(r"\W+", " ", t.lower()).strip()  # noqa: E731
            if any(norm(version.title or content.title) == norm(a.title) for a in arts):
                add(_r("title", "Заголовок", "warn", "Заголовок совпадает с заголовком источника — переформулируйте", blocks=True))

        # 4. атрибуция и собственные выводы
        need_attr = [f for f in fb.facts if f.attributed_to]
        if need_attr and not any(f.attributed_to.lower() in body_only.lower() or "по данным" in body_only.lower() for f in need_attr):
            add(_r("attribution", "Атрибуция", "warn", "Факты из единственного источника должны сопровождаться ссылкой на источник", blocks=True))
        else:
            add(_r("attribution", "Атрибуция", "pass", "Источники указаны"))
        has_sources_line = any(s.get("name", "") and s["name"].lower() in (rendered.text.lower()) for s in fb.sources) or not fb.sources
        add(_r("sources_listed", "Список источников", "pass" if has_sources_line else "warn", "Источники перечислены" if has_sources_line else "Не указаны источники публикации", blocks=not has_sources_line))
        opinion_hits = [m for m in know.matched("opinion_in_own_text", body_only)]
        if opinion_hits and know.count("attribution_markers", body_only):
            add(_r("own_opinion", "Свои выводы не приписаны источникам", "warn", f"Оценочные слова рядом с атрибуцией: {', '.join(opinion_hits[:3])}", blocks=True))
        else:
            add(_r("own_opinion", "Свои выводы не приписаны источникам", "pass", "Оценочных слов с атрибуцией не найдено"))

        # 5. политика и чувствительные темы
        sens_topics = dict((content.sensitivity or {}).get("topics", {}))
        political = bool((content.sensitivity or {}).get("political"))
        agit = know.matched("political_agitation", body_only)
        if agit:
            add(_r("politics", "Нейтральность", "fail", f"Агитационные формулировки/прогнозы выборов: {', '.join(agit[:3])}"))
        elif political or "elections" in sens_topics:
            add(_r("politics", "Нейтральность", "warn", "Политическая тема: только ручное подтверждение; проверьте нейтральность и атрибуцию спорных утверждений", blocks=True))
        else:
            add(_r("politics", "Нейтральность", "pass", "Политической агитации нет"))
        if sens_topics or content.requires_manual:
            add(_r("sensitive", "Чувствительная тема", "warn", "Тема требует ручного подтверждения: " + ", ".join(sens_topics) if sens_topics else "Требуется ручное подтверждение", blocks=True))
        forbidden = [w for w in (ctx.profile.get_settings(s, content.project_id).get("forbidden_words", []) or []) if w.lower() in rendered.text.lower()]
        add(_r("forbidden", "Запрещённые слова профиля", "fail" if forbidden else "pass", ", ".join(forbidden) if forbidden else "Нет"))

        # 6. язык, секреты, маркировка
        lang_ok = len(body_only) < 80 or detect_language(body_only) == version.language
        add(_r("language", "Язык", "pass" if lang_ok else "warn", "Язык текста совпадает" if lang_ok else f"Текст написан не на языке «{version.language}»", blocks=not lang_ok))
        if redact(rendered.text) != rendered.text:
            add(_r("secrets", "Секреты в тексте", "fail", "В тексте обнаружен токен/ключ"))
        else:
            add(_r("secrets", "Секреты в тексте", "pass", "Токенов нет"))
        if cfg.content.ai_disclosure:
            add(_r("disclosure", "Маркировка ИИ", "pass" if rendered.disclosure else "fail", rendered.disclosure or "Маркировка включена в настройках, но не добавлена"))
        else:
            add(_r("disclosure", "Маркировка ИИ", "warn", "Маркировка ИИ отключена в настройках проекта (закон РК «Об ИИ» требует маркировки синтетического контента)", blocks=True))

        # 7. медиа
        assets = {a.id: a for a in s.scalars(select(MediaAsset).where(MediaAsset.id.in_([m["asset_id"] for m in (version.media or [])] or [0])))}
        add(self._media_check(platform, version, assets, cfg))

        # 8. проверка события и режима генерации
        if ev is not None:
            vs = ev.verification_status
            if vs == "rejected":
                add(_r("verification", "Проверка фактов", "fail", "Событие не подтверждено"))
            elif vs in ("confirmed", "multi_confirmed"):
                ok = vs == "multi_confirmed" or cfg.autopilot.min_verification == "confirmed"
                add(_r("verification", "Проверка фактов", "pass", ev.verification_status, blocks=not ok))
            else:
                add(_r("verification", "Проверка фактов", "warn", "Требуется дополнительная проверка", blocks=True))
            if (ev.flags or {}).get("injection"):
                add(_r("injection", "Безопасность источника", "warn", "В источнике найдены инструкции для ИИ (prompt-injection)", blocks=True))
            if (ev.flags or {}).get("repeat_kind") == "duplicate":
                add(_r("repeat", "Повтор темы", "warn", f"Тема уже публиковалась ({ev.flags.get('repeat_of')})", blocks=True))
        if (content.generator or "heuristic").startswith("heuristic") and cfg.autopilot.require_llm_generator:
            add(_r("generator", "Генератор текста", "warn", "Текст составлен эвристикой (без LLM) — автопубликация запрещена, нужна редактура человеком", blocks=True))

        failed = [x for x in results if x["status"] == "fail"]
        blocks = any(x["blocks_autopilot"] for x in results)
        report = CheckReport(
            content_pk=content.id, platform_version_id=version.id, platform=platform, run_at=ctx.clock.now(), passed=not failed, blocks_autopilot=blocks, results=results,
            summary=(f"Не пройдено: {', '.join(x['label'] for x in failed)}" if failed else ("Предупреждения: " + ", ".join(x["label"] for x in results if x["status"] == "warn")[:300] if any(x["status"] == "warn" for x in results) else "Все проверки пройдены")),
        )  # fmt: skip
        s.add(report)
        s.flush()
        return report

    def _media_check(self, platform: str, version: PlatformVersion, assets: dict[int, MediaAsset], cfg: ProjectSettings) -> dict[str, Any]:
        need_media = platform == "instagram"
        if need_media and not assets:
            return _r("media", "Медиа", "fail", "Для Instagram нужно изображение или видео")
        problems: list[str] = []
        warns: list[str] = []
        for a in assets.values():
            if a.rights_status not in ALLOWED_RIGHTS:
                problems.append(f"права на изображение не подтверждены ({a.rights_status})")
            if a.rights_status in ("official_free", "licensed") and not a.attribution:
                warns.append("не указана атрибуция изображения")
            if a.is_ai_generated and not a.ai_label_applied:
                problems.append("ИИ-изображение без метки «Создано ИИ»")
            if platform == "instagram":
                if a.kind in ("image", "card") and a.mime != "image/jpeg":
                    problems.append("Instagram принимает только JPEG")
                if a.width and a.height:
                    ratio = a.width / a.height
                    if not (0.8 <= ratio <= 1.91):
                        problems.append(f"соотношение сторон {ratio:.2f} вне допустимого диапазона 4:5–1,91:1")
                if a.size_bytes > 8 * 1024 * 1024:
                    problems.append("файл больше 8 МБ")
        if platform == "instagram" and version.format == "carousel" and len(assets) > LIMITS["instagram"]["carousel_max"]:
            problems.append("в карусели не более 10 элементов")
        if problems:
            return _r("media", "Медиа", "fail", "; ".join(problems[:3]))
        if warns:
            return _r("media", "Медиа", "warn", "; ".join(warns), blocks=True)
        return _r("media", "Медиа", "pass", f"{len(assets)} файл(ов), права подтверждены" if assets else "Медиа не требуется")


__all__ = ["CheckService", "factbase_from_content", "extract_numbers"]
