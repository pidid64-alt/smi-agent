from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from ...container import Container
from ...core.errors import Forbidden, NotFound, ValidationFailed
from ...db.models import Notification, Project, User
from ...security.auth import Principal
from ...security.rbac import Actor, Perm, authorize
from ...settings_model import ProjectSettings, load_project_settings
from ..deps import get_ctx, get_principal, limit, project_actor, require

router = APIRouter(tags=["project"])


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class ProjectIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    name: str = Field(min_length=2, max_length=120)


class UserIn(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)
    display_name: str = Field(default="", max_length=120)
    role: str = "user"


class RoleIn(BaseModel):
    role: str | None = None


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_days: int | None = Field(default=90, ge=1, le=730)


@router.get("/projects")
def projects(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    with ctx.db.read() as s:
        return {"projects": ctx.auth.memberships(s, principal.user_id) if principal.kind == "human" else [{"project_id": principal.token_project_id}]}


@router.post("/projects", dependencies=[Depends(limit("project_create", 10, 60))])
def create_project(body: ProjectIn, principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    if not principal.is_superadmin:
        raise Forbidden("Создавать проекты может только суперадминистратор")
    actor = Actor.user(principal.user_id, "admin")
    with ctx.db.session() as s:
        if s.scalar(select(Project.id).where(Project.slug == body.slug)):
            raise ValidationFailed("Проект с таким идентификатором уже существует")
        p = Project(slug=body.slug, name=body.name, settings={})
        s.add(p)
        s.flush()
        n = ctx.ingest.seed_sources(s, p.id, actor, extra_yaml=ctx.settings.config_dir / "sources.yaml")
        ctx.audit.log(s, actor, "project.create", project_id=p.id, target_type="project", target_id=p.id, details={"slug": body.slug, "sources_seeded": n})
        return {"id": p.id, "slug": p.slug, "name": p.name, "sources_seeded": n}


@router.get("/p/{project_id}/settings")
def get_settings(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.read() as s:
        return load_project_settings(s.get(Project, project_id).settings).model_dump(mode="json")


@router.put("/p/{project_id}/settings")
def put_settings(project_id: int, patch: dict[str, Any], actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.SETTINGS_EDIT)
    if "mode" in patch:
        raise ValidationFailed("Режим агента меняется отдельным действием (Автопилот → Режим): это осознанный шаг")
    with ctx.db.session() as s:
        p = s.get(Project, project_id)
        merged = _deep_merge(load_project_settings(p.settings).model_dump(mode="json"), patch)
        try:
            ProjectSettings.model_validate(merged)
        except PydanticValidationError as e:
            raise ValidationFailed("Некорректные настройки: " + "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()[:4])) from e
        p.settings = merged
        ctx.audit.log(s, actor, "settings.update", project_id=project_id, details={"keys": sorted(patch)})
        return merged


# ---------------------------------------------------------------------------- пользователи и токены
@router.get("/p/{project_id}/users")
def users(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.USERS_MANAGE)
    from ...db.models import Membership

    with ctx.db.read() as s:
        rows = s.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id).where(Membership.project_id == project_id, User.kind == "human")).all()
        return {"users": [{"id": u.id, "username": u.username, "display_name": u.display_name, "role": role, "mfa_enabled": u.mfa_enabled, "is_active": u.is_active, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None} for u, role in rows]}


@router.post("/p/{project_id}/users", dependencies=[Depends(limit("user_create", 20, 60))])
def create_user(project_id: int, body: UserIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        u = ctx.auth.create_user(s, actor, username=body.username, password=body.password, display_name=body.display_name, project_id=project_id, role=body.role)
        return {"id": u.id, "username": u.username}


@router.put("/p/{project_id}/users/{user_id}/role")
def set_role(project_id: int, user_id: int, body: RoleIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        ctx.auth.set_membership(s, actor, project_id, user_id, body.role)
    return {"ok": True}


@router.get("/p/{project_id}/tokens")
def list_tokens(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.USERS_MANAGE)
    with ctx.db.read() as s:
        return {"tokens": ctx.auth.list_api_tokens(s, project_id)}


@router.post("/p/{project_id}/tokens")
def create_token(project_id: int, body: TokenIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        token, row = ctx.auth.create_api_token(s, actor, project_id, body.name, expires_days=body.expires_days)
        return {"id": row.id, "token": token, "prefix": row.prefix, "note": "Токен показывается один раз — сохраните его в менеджере секретов."}


@router.delete("/p/{project_id}/tokens/{token_id}")
def revoke_token(project_id: int, token_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        ctx.auth.revoke_api_token(s, actor, project_id, token_id)
    return {"ok": True}


# -------------------------------------------------------------------------------- источники
class SourceIn(BaseModel):
    model_config = {"extra": "allow"}
    key: str | None = Field(default=None, max_length=64)


@router.get("/p/{project_id}/sources")
def sources(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        return {"sources": ctx.ingest.source_health(s, project_id)}


@router.post("/p/{project_id}/sources")
def add_source(project_id: int, body: dict[str, Any], actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.SOURCES_MANAGE)
    with ctx.db.session() as s:
        src = ctx.ingest.upsert_source(s, project_id, actor, body)
        return {"id": src.id, "key": src.key}


@router.put("/p/{project_id}/sources/{source_id}")
def update_source(project_id: int, source_id: int, body: dict[str, Any], actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.SOURCES_MANAGE)
    with ctx.db.session() as s:
        src = ctx.ingest.upsert_source(s, project_id, actor, body, source_id=source_id)
        return {"id": src.id, "key": src.key, "enabled": src.enabled}


@router.delete("/p/{project_id}/sources/{source_id}")
def delete_source(project_id: int, source_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.SOURCES_MANAGE)
    with ctx.db.session() as s:
        ctx.ingest.delete_source(s, project_id, actor, source_id)
    return {"ok": True}


@router.post("/p/{project_id}/sources/{source_id}/poll", dependencies=[Depends(limit("poll", 30, 60))])
def poll_source(project_id: int, source_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.MONITOR_RUN)
    r = ctx.ingest.poll_source(project_id, source_id)
    return {"source": r.source_key, "ok": r.ok, "status": r.status, "fetched": r.fetched, "new": r.new}


# ---------------------------------------------------------------------------- оповещения
@router.get("/p/{project_id}/notifications")
def notifications(project_id: int, unread: bool = False, limit_: int = Query(50, alias="limit", le=200), actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.SYSTEM_VIEW)
    with ctx.db.read() as s:
        q = select(Notification).where((Notification.project_id == project_id) | (Notification.project_id.is_(None))).order_by(Notification.id.desc()).limit(limit_)
        if unread:
            q = q.where(Notification.read_at.is_(None))
        return {"notifications": [{"id": n.id, "level": n.level, "kind": n.kind, "title": n.title, "body": n.body, "created_at": n.created_at.isoformat(), "read": n.read_at is not None, "payload": n.payload} for n in s.scalars(q)]}


@router.post("/p/{project_id}/notifications/read")
def mark_read(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.SYSTEM_VIEW)
    with ctx.db.session() as s:
        for n in s.scalars(select(Notification).where((Notification.project_id == project_id) | (Notification.project_id.is_(None)), Notification.read_at.is_(None))):
            n.read_at = ctx.clock.now()
    return {"ok": True}


# ------------------------------------------------------------------------------------ аудит
@router.get("/p/{project_id}/audit")
def audit(project_id: int, action: str | None = None, actor_id: str | None = None, limit_: int = Query(100, alias="limit", le=500), offset: int = 0, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AUDIT_VIEW)
    from ...audit.service import row_to_dict

    with ctx.db.read() as s:
        return {"entries": [row_to_dict(r) for r in ctx.audit.query(s, project_id=project_id, action=action, actor=actor_id, limit=limit_, offset=offset)]}


@router.get("/p/{project_id}/audit/verify")
def audit_verify(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AUDIT_VIEW)
    with ctx.db.read() as s:
        r = ctx.audit.verify_chain(s)
        return {"ok": r.ok, "checked": r.checked, "first_bad_id": r.first_bad_id, "reason": r.reason}


@router.get("/p/{project_id}/audit/export")
def audit_export(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AUDIT_EXPORT)

    def gen():
        with ctx.db.read() as s:
            for row in ctx.audit.export(s, project_id=project_id):
                yield json.dumps(row, ensure_ascii=False, default=str) + "\n"

    with ctx.db.session() as s:
        ctx.audit.log(s, actor, "audit.export", project_id=project_id)
    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Content-Disposition": "attachment; filename=audit.ndjson"})


# ------------------------------------------------------------------------------ здоровье, копии
@router.get("/health")
def health(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    with ctx.db.read() as s:
        if principal.kind == "human" and not principal.is_superadmin and not ctx.auth.memberships(s, principal.user_id):
            raise Forbidden("Нет доступа")
    return ctx.health.check(record=False)


@router.get("/metrics")
def metrics(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    from fastapi.responses import PlainTextResponse

    if not principal.is_superadmin and principal.kind != "service":
        raise Forbidden("Только суперадминистратор или сервисный токен")
    return PlainTextResponse(ctx.health.prometheus())


@router.get("/backups")
def backups(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    if not principal.is_superadmin:
        raise Forbidden("Только суперадминистратор")
    return {"backups": ctx.backup.list(), "rpo_minutes": ctx.settings.backup_interval_min}


@router.post("/backups", dependencies=[Depends(limit("backup", 6, 60))])
def create_backup(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    if not principal.is_superadmin:
        raise Forbidden("Только суперадминистратор")
    res = ctx.backup.create(note=f"ручная: {principal.username}")
    with ctx.db.session() as s:
        ctx.audit.log(s, Actor.user(principal.user_id, "admin"), "backup.create", details={"id": res["id"], "size": res["size_bytes"]})
    return {k: v for k, v in res.items() if k != "path"}


@router.post("/backups/restore-test", dependencies=[Depends(limit("backup", 6, 60))])
def restore_test(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    if not principal.is_superadmin:
        raise Forbidden("Только суперадминистратор")
    r = ctx.backup.restore_test()
    return {"ok": r["ok"], "rto_seconds": r["rto_seconds"], "integrity": r["verify"].get("integrity"), "audit_chain_ok": r["verify"].get("audit_chain_ok")}


__all__ = ["router", "NotFound", "authorize"]
