"""Создание материала из выбранного предложения: факты → текст → версии для платформ → визуал → проверки (ТЗ §20–23, §30–32)."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import Platform
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..core.ids import next_content_id
from ..core.text import sha256_hex
from ..db.models import CheckReport, Content, Event, MediaAsset, PlatformVersion, Project, Proposal, Verification
from ..security.rbac import Actor
from ..settings_model import ProjectSettings, load_project_settings
from ..visual.cards import CardRenderer, sha256_bytes, to_jpeg
from .factbase import FactBase, build_factbase
from ..core.text import clip
from .generator import Brief, ContentGenerator, CoreDraft
from .render import Rendered, render_final

log = logging.getLogger(__name__)


@dataclass
class Prepared:
    """Результат фазы подготовки: всё, что нужно записать, без обращений к БД на запись."""

    proposal_id: int
    project_id: int
    overrides: dict[str, Any]
    fb: FactBase
    brief: Brief
    core: CoreDraft
    platform_data: dict[str, dict[str, Any]]


ANGLE_EMPHASIS = {
    "kazakhstan": "Что это значит для Казахстана (только подтверждённые факты и без домыслов)",
    "numbers": "Акцент на цифрах: что измерено, как изменилось и откуда данные",
    "practical": "Практически: кого касается, что изменится и что делать людям",
    "world": "Глобальный контекст: почему это важно за пределами Казахстана",
}


def _factbase_items(fb: FactBase) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for f in fb.facts:
        items.append({"kind": "fact", "id": f.id, "text": f.text, "sources": f.sources, "support": f.support, "attributed_to": f.attributed_to, "numbers": f.numbers})
    for q in fb.quotes:
        items.append({"kind": "quote", "text": q.text, "speaker": q.speaker, "source": q.source, "source_name": q.source_name})
    for n in fb.all_numbers:
        items.append({"kind": "number", **n})
    return items


class ContentService:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.gen = ContentGenerator(ctx)
        self.cards = CardRenderer()

    # ------------------------------------------------------------------- бриф
    def make_brief(self, s: Session, project_id: int, cfg: ProjectSettings, card: dict[str, Any], overrides: dict[str, Any], fb: FactBase) -> Brief:
        prof = self.ctx.profile.get_settings(s, project_id)
        style = self.ctx.profile.style_hints(s, project_id)
        emphasis = overrides.get("emphasis", "")
        angle = overrides.get("angle") or ANGLE_EMPHASIS.get(emphasis) or card.get("angle", "")
        platforms = [p for p in (overrides.get("platforms") or cfg.content.platforms) if p in {x.value for x in Platform}]
        lang = overrides.get("language") or cfg.content.default_language
        return Brief(
            language=lang, tone=overrides.get("tone") or prof.get("tone", "neutral"), angle=angle, emphasis=emphasis, length=overrides.get("length", ""),
            length_multiplier=style["length_multiplier"], format_code=overrides.get("format") or (card.get("format") or {}).get("code", "post_card"),
            platforms=platforms or ["telegram"], max_hashtags={**prof.get("max_hashtags", {}), **({"telegram": 1, "instagram": 4, "facebook": 1} if style["fewer_hashtags"] else {})},
            default_hashtags=prof.get("default_hashtags", []), forbidden_words=prof.get("forbidden_words", []), signature=prof.get("signature", ""),
            political=fb.political, copy_max_run_words=cfg.content.copy_max_run_words, copy_max_ratio=cfg.content.copy_max_ratio,
            category_label=self.ctx.know.category_label(fb.category),
        )  # fmt: skip

    # ----------------------------------------------------------------- создание
    def prepare(self, s: Session, proposal_id: int, overrides: dict[str, Any]) -> Prepared:
        """Фаза 1+2: чтение данных и генерация текста (в т.ч. вызовы LLM). Без записи в БД и вне транзакции записи."""
        proposal = s.get(Proposal, proposal_id)
        if proposal is None:
            raise NotFound("Предложение не найдено")
        ev = s.get(Event, proposal.event_id)
        cfg = load_project_settings(s.get(Project, proposal.project_id).settings)
        ver = s.get(Verification, proposal.verification_id) if proposal.verification_id else None
        fb = build_factbase(s, ev, ver, know=self.ctx.know)
        if not fb.facts:
            raise ValidationFailed("Недостаточно проверенных фактов для материала — выберите другую тему или раскройте подробности")
        brief = self.make_brief(s, proposal.project_id, cfg, proposal.card or {}, overrides, fb)
        core = self.gen.core(fb, brief, project_id=proposal.project_id)
        platform_data = {platform: self.gen.adapt(platform, core, fb, brief) for platform in brief.platforms}
        return Prepared(proposal.id, proposal.project_id, dict(overrides), fb, brief, core, platform_data)

    def persist(self, s: Session, proposal: Proposal, prep: Prepared, actor: Actor, *, origin: str = "user") -> Content:
        """Фаза 3: запись материала, версий, карточек и результатов проверок (короткая транзакция)."""
        ctx = self.ctx
        ev = s.get(Event, proposal.event_id)
        cfg = load_project_settings(s.get(Project, proposal.project_id).settings)
        fb, brief, core = prep.fb, prep.brief, prep.core
        now = ctx.clock.now()
        sens = {"topics": fb.sensitive, "political": fb.political}
        requires_manual = bool(fb.sensitive) or fb.political or bool((ev.flags or {}).get("sensitive_category"))
        repeat = (ev.flags or {}).get("repeat_kind")
        content = Content(
            content_id=next_content_id(s, now), project_id=proposal.project_id, proposal_id=proposal.id, event_id=ev.id, title=core.headline, category=ev.category, geo=ev.geo,
            subtopics=list(ev.subtopics or []), language=brief.language, angle=brief.angle[:300], status="draft", generator=core.generator,
            fact_base=_factbase_items(fb), sources=[{k: v for k, v in src.items()} for src in fb.sources],
            draft={"headline": core.headline, "lead": core.lead, "points": core.points, "context": core.context, "why_it_matters": core.why_it_matters, "format_code": brief.format_code, "notes": core.notes, "unknowns": fb.unknowns, "used_fact_ids": core.used_fact_ids},
            prediction=dict(proposal.prediction or {}), sensitivity=sens, requires_manual=requires_manual, origin=origin, version=1,
            supersedes_event_stage=(f"новая стадия: ранее {ev.flags.get('repeat_of')}" if repeat == "new_stage" else ""), created_by=actor.label, created_at=now, updated_at=now,
        )  # fmt: skip
        s.add(content)
        s.flush()
        for platform, data in prep.platform_data.items():
            media = self._make_media(s, proposal.project_id, content, platform, data["format"], core, fb, cfg, ev)
            s.add(PlatformVersion(
                content_pk=content.id, platform=platform, version=1, is_current=True, format=data["format"], title=data.get("title", core.headline), body=data["body"], hashtags=data.get("hashtags", []),
                media=media, extras=data.get("extras", {}), language=brief.language, text_hash=sha256_hex(data["body"]), created_by="ai" if core.generator != "heuristic" else "heuristic", created_at=now,
            ))  # fmt: skip
        s.flush()
        proposal.content_pk = content.id
        self.run_checks(s, content)
        ctx.audit.log(s, actor, "content.create", project_id=proposal.project_id, target_type="content", target_id=content.content_id, details={"generator": core.generator, "platforms": brief.platforms, "event_id": ev.id})
        return content

    def create(self, project_id: int, proposal_id: int, overrides: dict[str, Any], actor: Actor, *, origin: str = "user") -> int:
        """Трёхфазное создание материала без внешней сессии: чтение → LLM (без транзакции) → запись. Возвращает content.id."""
        with self.ctx.db.read() as rs:
            prep = self.prepare(rs, proposal_id, overrides)
        with self.ctx.db.session() as ws:
            proposal = ws.get(Proposal, proposal_id)
            return self.persist(ws, proposal, prep, actor, origin=origin).id

    def create_from_proposal(self, s: Session, proposal: Proposal, overrides: dict[str, Any], actor: Actor, *, origin: str = "user") -> Content:
        """Удобная обёртка для кода, который уже держит сессию (тесты, эвристический режим). С включённой LLM внутри
        транзакции записи вызывать нельзя — используйте create()."""
        prep = self.prepare(s, proposal.id, overrides)
        return self.persist(s, proposal, prep, actor, origin=origin)

    # ---------------------------------------------------------------------- медиа
    def _save_card(self, s: Session, project_id: int, img, alt: str, *, source_article_id: int | None = None) -> MediaAsset:
        settings = self.ctx.settings
        data = to_jpeg(img)
        token = secrets.token_urlsafe(24)
        year = self.ctx.clock.now().strftime("%Y")
        rel = Path(year) / f"{token}.jpg"
        path = Path(settings.media_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        asset = MediaAsset(
            project_id=project_id, kind="card", storage_path=str(rel), public_token=token, mime="image/jpeg", size_bytes=len(data), width=img.width, height=img.height,
            sha256=sha256_bytes(data), source_article_id=source_article_id, rights_status="own", license="Собственная графика проекта", attribution="", is_ai_generated=False,
            ai_label_applied=False, alt_text=alt[:500], created_at=self.ctx.clock.now(),
        )  # fmt: skip
        s.add(asset)
        s.flush()
        return asset

    def _make_media(self, s: Session, project_id: int, content: Content, platform: str, fmt: str, core: CoreDraft, fb: FactBase, cfg: ProjectSettings, ev: Event) -> list[dict[str, Any]]:
        brand = cfg.content.brand_name
        cat = self.ctx.know.category_label(ev.category)
        geo = {"kz": "Казахстан", "ca": "Центральная Азия", "world": "Мир"}.get(ev.geo, "")
        kicker = f"{cat} · {geo}" if geo else cat
        names = ", ".join(src["name"] for src in fb.sources[:3])
        footer = f"Источники: {names}" if names else ""
        out: list[dict[str, Any]] = []
        if platform == "instagram":
            size = "portrait"
            cover = self._save_card(s, project_id, self.cards.cover(core.headline, kicker=kicker, brand=brand, category=ev.category, size=size, footer=footer), core.headline)
            out.append({"asset_id": cover.id, "role": "cover"})
            if fmt == "carousel":
                numbered = [f for f in fb.facts if f.numbers][:2]
                used = set()
                for f in numbered:
                    n = f.numbers[0]
                    img = self.cards.stat(n["raw"], clip(f.text, 90), kicker="Цифра", brand=brand, category=ev.category, size=size)
                    out.append({"asset_id": self._save_card(s, project_id, img, f"{n['raw']}: {f.text}"[:400]).id, "role": "stat"})
                    used.add(f.id)
                for p in core.points:
                    if len(out) >= 8:
                        break
                    if any(clip(f.text, 90)[:20] in p for f in numbered):
                        continue
                    img = self.cards.text_card(p, kicker="Что известно", brand=brand, category=ev.category, size=size)
                    out.append({"asset_id": self._save_card(s, project_id, img, p).id, "role": "slide"})
                lang_label = cfg.content.ai_disclosure_text.get(core.language, "")
                img = self.cards.sources_card([src["name"] for src in fb.sources[:5]], lang_label, kicker="Источники", brand=brand, category=ev.category, size=size)
                out.append({"asset_id": self._save_card(s, project_id, img, "Источники материала").id, "role": "sources"})
        elif platform == "telegram" and fmt in ("photo", "gallery"):
            img = self.cards.cover(core.headline, kicker=kicker, brand=brand, category=ev.category, size="wide", footer=footer)
            out.append({"asset_id": self._save_card(s, project_id, img, core.headline).id, "role": "cover"})
        elif platform == "facebook" and fmt in ("photo", "link"):
            img = self.cards.cover(core.headline, kicker=kicker, brand=brand, category=ev.category, size="wide", footer=footer)
            out.append({"asset_id": self._save_card(s, project_id, img, core.headline).id, "role": "cover"})
        return out

    # -------------------------------------------------------------------- проверки
    def run_checks(self, s: Session, content: Content, *, reviewed: bool = False) -> list[CheckReport]:
        reports = []
        for v in self.current_versions(s, content.id):
            reports.append(self.ctx.checks.run(s, content, v, reviewed=reviewed))
        return reports

    def current_versions(self, s: Session, content_pk: int) -> list[PlatformVersion]:
        return list(s.scalars(select(PlatformVersion).where(PlatformVersion.content_pk == content_pk, PlatformVersion.is_current.is_(True)).order_by(PlatformVersion.platform)))

    def latest_report(self, s: Session, version_id: int) -> CheckReport | None:
        return s.scalars(select(CheckReport).where(CheckReport.platform_version_id == version_id).order_by(CheckReport.id.desc()).limit(1)).first()

    # ---------------------------------------------------------------- правки пользователя
    def edit_version(self, s: Session, project_id: int, content_pk: int, platform: str, actor: Actor, *, body: str | None = None, title: str | None = None, hashtags: list[str] | None = None) -> PlatformVersion:
        content = s.get(Content, content_pk)
        if content is None or content.project_id != project_id:
            raise NotFound("Материал не найден")
        cur = s.scalars(select(PlatformVersion).where(PlatformVersion.content_pk == content_pk, PlatformVersion.platform == platform, PlatformVersion.is_current.is_(True))).first()
        if cur is None:
            raise NotFound("Версия для платформы не найдена")
        if body is not None and not body.strip():
            raise ValidationFailed("Текст не может быть пустым")
        new = PlatformVersion(
            content_pk=content_pk, platform=platform, version=cur.version + 1, is_current=True, format=cur.format, title=title if title is not None else cur.title,
            body=body if body is not None else cur.body, hashtags=hashtags if hashtags is not None else list(cur.hashtags or []), media=list(cur.media or []), extras=dict(cur.extras or {}),
            language=cur.language, created_by=actor.label, created_at=self.ctx.clock.now(),
        )  # fmt: skip
        new.text_hash = sha256_hex(new.body)
        cur.is_current = False
        s.add(new)
        s.flush()
        if body is not None:
            uid = int(actor.id) if actor.type == "user" and actor.id.isdigit() else None
            self.ctx.learning.record_edit(s, project_id, content, platform, "body", cur.body, new.body, user_id=uid)
        content.updated_at = self.ctx.clock.now()
        content.version += 1
        self.ctx.checks.run(s, content, new)
        self.ctx.audit.log(s, actor, "content.edit", project_id=project_id, target_type="content", target_id=content.content_id, details={"platform": platform, "version": new.version})
        # публикации на старой версии требуют повторного подтверждения
        if hasattr(self.ctx, "publishing"):
            self.ctx.publishing.on_version_changed(s, cur, new, actor)
        return new

    # ------------------------------------------------------------------- чтение
    def preview(self, s: Session, content: Content, platform: str, *, reviewed: bool = False) -> Rendered:
        cfg = load_project_settings(s.get(Project, content.project_id).settings)
        v = next((x for x in self.current_versions(s, content.id) if x.platform == platform), None)
        if v is None:
            raise NotFound("Версия для платформы не найдена")
        return render_final(platform, v, cfg, reviewed=reviewed)

    def to_dict(self, s: Session, content: Content) -> dict[str, Any]:
        cfg = load_project_settings(s.get(Project, content.project_id).settings)
        versions = []
        for v in self.current_versions(s, content.id):
            rep = self.latest_report(s, v.id)
            rend = render_final(v.platform, v, cfg, reviewed=False)
            versions.append({
                "id": v.id, "platform": v.platform, "version": v.version, "format": v.format, "title": v.title, "body": v.body, "hashtags": v.hashtags, "extras": v.extras, "language": v.language,
                "media": [{"asset_id": m["asset_id"], "role": m["role"]} for m in (v.media or [])], "final_text": rend.text, "length": rend.length, "limit": rend.limit,
                "checks": {"passed": rep.passed, "blocks_autopilot": rep.blocks_autopilot, "summary": rep.summary, "results": rep.results, "run_at": rep.run_at.isoformat()} if rep else None,
            })  # fmt: skip
        return {
            "id": content.id, "content_id": content.content_id, "title": content.title, "category": content.category, "geo": content.geo, "language": content.language, "angle": content.angle,
            "status": content.status, "generator": content.generator, "requires_manual": content.requires_manual, "origin": content.origin, "event_id": content.event_id,
            "sources": content.sources, "draft": content.draft, "prediction": content.prediction, "sensitivity": content.sensitivity, "created_at": content.created_at.isoformat(),
            "supersedes_event_stage": content.supersedes_event_stage, "versions": versions,
        }  # fmt: skip

    def list(self, s: Session, project_id: int, *, limit: int = 50, offset: int = 0) -> list[Content]:
        return list(s.scalars(select(Content).where(Content.project_id == project_id).order_by(Content.id.desc()).limit(limit).offset(offset)))

    def get(self, s: Session, project_id: int, content_pk: int) -> Content:
        c = s.get(Content, content_pk)
        if c is None or c.project_id != project_id:
            raise NotFound("Материал не найден")
        return c


__all__ = ["ContentService", "Conflict"]
