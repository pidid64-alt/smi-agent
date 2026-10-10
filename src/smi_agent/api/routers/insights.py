from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...container import Container
from ...core.errors import NotFound
from ...db.models import Report
from ...security.rbac import Actor, Perm
from ..deps import get_ctx, limit, project_actor, require

router = APIRouter(tags=["analytics"])


class ImportIn(BaseModel):
    rows: list[dict[str, Any]] = Field(max_length=1000)
    source: str = Field(default="import", pattern="^(import|manual)$")


class ReportIn(BaseModel):
    kind: str = Field(pattern="^(weekly|monthly|strategy)$")


@router.get("/p/{project_id}/dashboard")
def dashboard(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW if actor.role != "auditor" else Perm.ANALYTICS_VIEW)
    with ctx.db.read() as s:
        return ctx.analytics.dashboard(s, project_id)


@router.get("/p/{project_id}/analytics/series")
def series(project_id: int, metric: str = "posts", period: str = "7d", start: datetime | None = None, end: datetime | None = None, platform: str | None = None, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.ANALYTICS_VIEW)
    with ctx.db.read() as s:
        return ctx.analytics.series(s, project_id, metric, period, start=start, end=end, platform=platform)


@router.get("/p/{project_id}/analytics/geo")
def geo(project_id: int, days: int = Query(30, ge=1, le=800), actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.ANALYTICS_VIEW)
    from datetime import timedelta

    now = ctx.clock.now()
    with ctx.db.read() as s:
        return ctx.analytics.geo_ratio(s, project_id, now - timedelta(days=days), now)


@router.get("/p/{project_id}/analytics/insights")
def insights(project_id: int, days: int = Query(90, ge=7, le=800), actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.ANALYTICS_VIEW)
    with ctx.db.read() as s:
        return ctx.analytics.insights(s, project_id, days=days)


@router.get("/p/{project_id}/analytics/forecast")
def forecast(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.ANALYTICS_VIEW)
    with ctx.db.read() as s:
        return ctx.metrics.forecast_accuracy(s, project_id)


@router.get("/p/{project_id}/analytics/posts/{pub_id}")
def post_stats(project_id: int, pub_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.ANALYTICS_VIEW)
    with ctx.db.read() as s:
        return ctx.metrics.post_stats(s, project_id, pub_id)


@router.post("/p/{project_id}/analytics/import", dependencies=[Depends(limit("import", 12, 60))])
def import_metrics(project_id: int, body: ImportIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    """Ручной ввод/импорт статистики (у Telegram Bot API нет статистики постов канала)."""
    require(actor, Perm.CONTENT_EDIT)
    with ctx.db.session() as s:
        return ctx.metrics.import_rows(s, project_id, actor, body.rows, source=body.source)


@router.get("/p/{project_id}/reports")
def reports(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.REPORTS_VIEW)
    with ctx.db.read() as s:
        return {"reports": [{"id": r.id, "kind": r.kind, "period_start": r.period_start.isoformat(), "period_end": r.period_end.isoformat(), "created_at": r.created_at.isoformat()} for r in s.scalars(select(Report).where(Report.project_id == project_id).order_by(Report.id.desc()).limit(50))]}


@router.get("/p/{project_id}/reports/{report_id}")
def report(project_id: int, report_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.REPORTS_VIEW)
    with ctx.db.read() as s:
        r = s.get(Report, report_id)
        if r is None or r.project_id != project_id:
            raise NotFound("Отчёт не найден")
        return {"id": r.id, "kind": r.kind, "markdown": r.markdown, "payload": r.payload, "created_at": r.created_at.isoformat()}


@router.post("/p/{project_id}/reports", dependencies=[Depends(limit("reports", 6, 60))])
def build_report(project_id: int, body: ReportIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.REPORTS_VIEW)
    with ctx.db.session() as s:
        r = ctx.analytics.build_report(s, project_id, body.kind)
        return {"id": r.id, "kind": r.kind}
