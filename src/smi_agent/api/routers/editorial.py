from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...container import Container
from ...core.errors import NotFound
from ...db.models import Content, Event, MediaAsset, Proposal
from ...security.rbac import Actor, Perm
from ..deps import get_ctx, limit, project_actor, require

router = APIRouter(tags=["editorial"])


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class SelectIn(BaseModel):
    emphasis: str | None = Field(default=None, max_length=60)
    language: str | None = Field(default=None, pattern="^(ru|kk|en)$")
    format: str | None = Field(default=None, max_length=30)
    length: str | None = Field(default=None, pattern="^(shorter|longer)$")
    tone: str | None = Field(default=None, pattern="^(neutral|friendly|formal)$")
    platforms: list[str] | None = None
    angle: str | None = Field(default=None, max_length=300)


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=300)


class AngleIn(BaseModel):
    angle: str | None = Field(default=None, max_length=300)


class EditIn(BaseModel):
    body: str | None = Field(default=None, max_length=70000)
    title: str | None = Field(default=None, max_length=600)
    hashtags: list[str] | None = None


class PublishIn(BaseModel):
    platforms: list[str] | None = None
    schedule: dict[str, Any] | None = None


class ApproveIn(BaseModel):
    schedule: dict[str, Any] | None = None


class RetryIn(BaseModel):
    confirm_not_published: bool = False


class ConfirmIn(BaseModel):
    url: str = Field(default="", max_length=500)


# ------------------------------------------------------------------------------- повестка
@router.get("/p/{project_id}/events")
def events(project_id: int, stage: str | None = None, category: str | None = None, limit_: int = Query(50, alias="limit", le=200), actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        q = select(Event).where(Event.project_id == project_id, Event.merged_into_id.is_(None)).order_by(Event.trend_score.desc()).limit(limit_)
        if stage:
            q = q.where(Event.stage == stage)
        if category:
            q = q.where(Event.category == category)
        return {"events": [{"id": e.id, "title": e.title, "category": e.category, "category_label": ctx.know.category_label(e.category), "geo": e.geo, "trend_score": e.trend_score, "phase": e.phase, "velocity": e.velocity, "n_independent": e.n_independent, "n_articles": e.n_articles, "verification": e.verification_status, "stage": e.stage, "last_update_at": e.last_update_at.isoformat()} for e in s.scalars(q)]}


@router.get("/p/{project_id}/events/{event_id}")
def event_detail(project_id: int, event_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        return ctx.events.detail(s, project_id, event_id)


def _pipeline(ctx: Container, project_id: int) -> None:
    ctx.ingest.poll_due(project_id)
    ctx.events.process_new(project_id)
    with ctx.db.session() as s:
        ctx.scoring.score_recent(s, project_id)


@router.post("/p/{project_id}/pipeline/run", dependencies=[Depends(limit("pipeline", 6, 60))])
def run_pipeline(project_id: int, background: BackgroundTasks, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    """Сбор → кластеризация → оценка. Выполняется в фоне (опрос источников может занять время)."""
    require(actor, Perm.FUNNEL_RUN)
    background.add_task(_pipeline, ctx, project_id)
    return {"started": True}


@router.post("/p/{project_id}/funnel/run", dependencies=[Depends(limit("funnel", 6, 60))])
def run_funnel(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.FUNNEL_RUN)
    ctx.events.process_new(project_id)
    with ctx.db.session() as s:
        run = ctx.funnel.run(s, project_id, actor, trigger="manual")
        return {"run_id": run.id, "counts": run.counts, "geo_ratio": run.geo_ratio, "notes": run.notes}


# ---------------------------------------------------------------------------- предложения
@router.get("/p/{project_id}/proposals")
def proposals(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PROPOSALS_VIEW)
    with ctx.db.read() as s:
        return {"proposals": [{"id": p.id, "slot": p.slot, "status": p.status, "card": p.card, "expanded": p.expanded or None, "content_pk": p.content_pk, "overrides": p.overrides} for p in ctx.interaction.current(s, project_id)]}


@router.post("/p/{project_id}/commands", dependencies=[Depends(limit("commands", 60, 60))])
def commands(project_id: int, body: CommandIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    """Текстовые команды: «Беру №2», «№4, но сделай акцент на Казахстан», «№1 неинтересна», «Замени №3», «Раскрой №5 подробнее»."""
    require(actor, Perm.PROPOSALS_ACT)
    return ctx.interaction.handle_text(project_id, actor, body.text)


@router.post("/p/{project_id}/proposals/{slot}/select", dependencies=[Depends(limit("commands", 60, 60))])
def select(project_id: int, slot: int, body: SelectIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PROPOSALS_ACT)
    return ctx.interaction.select(project_id, actor, slot, {k: v for k, v in body.model_dump().items() if v}).__dict__


@router.post("/p/{project_id}/proposals/{slot}/reject")
def reject(project_id: int, slot: int, body: ReasonIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PROPOSALS_ACT)
    with ctx.db.session() as s:
        return ctx.interaction.reject(s, project_id, actor, slot, body.reason).__dict__


@router.post("/p/{project_id}/proposals/{slot}/replace")
def replace(project_id: int, slot: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PROPOSALS_ACT)
    with ctx.db.session() as s:
        return ctx.interaction.replace(s, project_id, actor, slot).__dict__


@router.post("/p/{project_id}/proposals/{slot}/more")
def more(project_id: int, slot: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PROPOSALS_ACT)
    with ctx.db.session() as s:
        return ctx.interaction.more_info(s, project_id, actor, slot).__dict__


@router.post("/p/{project_id}/proposals/{slot}/angle")
def angle(project_id: int, slot: int, body: AngleIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PROPOSALS_ACT)
    with ctx.db.session() as s:
        return ctx.interaction.change_angle(s, project_id, actor, slot, body.angle).__dict__


# -------------------------------------------------------------------------------- материалы
@router.get("/p/{project_id}/content")
def content_list(project_id: int, limit_: int = Query(50, alias="limit", le=200), offset: int = 0, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        return {"content": [{"id": c.id, "content_id": c.content_id, "title": c.title, "status": c.status, "category": c.category, "geo": c.geo, "generator": c.generator, "origin": c.origin, "requires_manual": c.requires_manual, "created_at": c.created_at.isoformat()} for c in ctx.content.list(s, project_id, limit=limit_, offset=offset)]}


@router.get("/p/{project_id}/content/{content_pk}")
def content_detail(project_id: int, content_pk: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        c = ctx.content.get(s, project_id, content_pk)
        d = ctx.content.to_dict(s, c)
        d["forecast"] = ctx.metrics.forecast_for(s, project_id, c.prediction or {})
        return d


@router.put("/p/{project_id}/content/{content_pk}/versions/{platform}")
def edit_version(project_id: int, content_pk: int, platform: str, body: EditIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.CONTENT_EDIT)
    with ctx.db.session() as s:
        v = ctx.content.edit_version(s, project_id, content_pk, platform, actor, body=body.body, title=body.title, hashtags=body.hashtags)
        return {"version": v.version, "platform": platform}


@router.post("/p/{project_id}/content/{content_pk}/checks")
def rerun_checks(project_id: int, content_pk: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.CHECK_RUN if actor.type == "service" else Perm.CONTENT_EDIT)
    with ctx.db.session() as s:
        c = ctx.content.get(s, project_id, content_pk)
        reps = ctx.content.run_checks(s, c)
        return {"reports": [{"platform": r.platform, "passed": r.passed, "blocks_autopilot": r.blocks_autopilot, "summary": r.summary, "results": r.results} for r in reps]}


@router.get("/p/{project_id}/content/{content_pk}/preview/{platform}")
def preview(project_id: int, content_pk: int, platform: str, reviewed: bool = False, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        c = ctx.content.get(s, project_id, content_pk)
        r = ctx.content.preview(s, c, platform, reviewed=reviewed)
        return {"platform": r.platform, "format": r.format, "text": r.text, "parse_mode": r.parse_mode, "length": r.length, "limit": r.limit, "too_long": r.too_long, "disclosure": r.disclosure}


@router.get("/p/{project_id}/assets/{asset_id}")
def asset(project_id: int, asset_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    from pathlib import Path

    with ctx.db.read() as s:
        a = s.get(MediaAsset, asset_id)
        if a is None or a.project_id != project_id:
            raise NotFound("Файл не найден")
        path = (Path(ctx.settings.media_dir) / a.storage_path).resolve()
        if Path(ctx.settings.media_dir).resolve() not in path.parents or not path.exists():
            raise NotFound("Файл не найден")
        return FileResponse(path, media_type=a.mime, headers={"Cache-Control": "private, max-age=300"})


# ------------------------------------------------------------------------------- публикации
@router.post("/p/{project_id}/content/{content_pk}/publications")
def create_publications(project_id: int, content_pk: int, body: PublishIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.CONTENT_EDIT)
    with ctx.db.session() as s:
        pubs = ctx.publishing.create_for_content(s, project_id, content_pk, actor, platforms=body.platforms, schedule=body.schedule)
        return {"publications": [ctx.publishing.to_dict(s, p) for p in pubs]}


@router.get("/p/{project_id}/publications")
def publications(project_id: int, state: str | None = None, limit_: int = Query(100, alias="limit", le=300), actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        return {"publications": [ctx.publishing.to_dict(s, p) for p in ctx.publishing.list(s, project_id, states=[state] if state else None, limit=limit_)]}


@router.get("/p/{project_id}/publications/{pub_id}")
def publication(project_id: int, pub_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        return ctx.publishing.to_dict(s, ctx.publishing.get(s, project_id, pub_id), detail=True)


@router.post("/p/{project_id}/publications/{pub_id}/approve")
def approve(project_id: int, pub_id: int, body: ApproveIn, background: BackgroundTasks, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        pub = ctx.publishing.approve(s, project_id, pub_id, actor, schedule=body.schedule)
        out = ctx.publishing.to_dict(s, pub)
    if (out.get("schedule") or {}).get("mode") == "now":
        background.add_task(ctx.publishing.publish_one, pub_id)  # не ждём воркер: отправка сразу после подтверждения
    return out


@router.post("/p/{project_id}/publications/{pub_id}/cancel")
def cancel(project_id: int, pub_id: int, body: ReasonIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.publishing.to_dict(s, ctx.publishing.cancel(s, project_id, pub_id, actor, body.reason))


@router.post("/p/{project_id}/publications/{pub_id}/retry")
def retry(project_id: int, pub_id: int, body: RetryIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.publishing.to_dict(s, ctx.publishing.retry(s, project_id, pub_id, actor, confirm_not_published=body.confirm_not_published))


@router.post("/p/{project_id}/publications/{pub_id}/reconcile")
def reconcile(project_id: int, pub_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PUBLISH_APPROVE)
    return ctx.publishing.reconcile(project_id, pub_id, actor)


@router.post("/p/{project_id}/publications/{pub_id}/confirm-published")
def confirm_published(project_id: int, pub_id: int, body: ConfirmIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.publishing.to_dict(s, ctx.publishing.confirm_published(s, project_id, pub_id, actor, body.url))


__all__ = ["router", "Content", "Proposal"]
