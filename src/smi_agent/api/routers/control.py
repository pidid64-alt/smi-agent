from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ...container import Container
from ...core.errors import ValidationFailed
from ...security.rbac import Actor, Perm
from ..deps import client_ip, get_ctx, limit, project_actor, require

router = APIRouter(tags=["control"])


class AccountIn(BaseModel):
    platform: str = Field(max_length=16)
    token: str = Field(default="", max_length=2000)
    external_id: str = Field(default="", max_length=120)
    display_name: str = Field(default="", max_length=200)
    mode: str = Field(default="manual", pattern="^(manual|scheduled|auto)$")
    sandbox: bool = False


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=2000)


class ModeIn(BaseModel):
    mode: str = Field(max_length=16)


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class PolicyIn(BaseModel):
    enabled: bool
    platform: str | None = Field(default=None, max_length=16)
    category: str | None = Field(default=None, max_length=40)
    constraints: dict[str, Any] | None = None


class KillIn(BaseModel):
    scope_type: str = Field(max_length=12)
    scope_value: str = Field(default="", max_length=80)
    reason: str = Field(default="", max_length=500)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=500)


# ------------------------------------------------------------------------------- аккаунты
@router.get("/p/{project_id}/accounts")
def accounts(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        return {"accounts": [ctx.accounts.to_dict(a) for a in ctx.accounts.list(s, project_id)]}


@router.post("/p/{project_id}/accounts", dependencies=[Depends(limit("accounts", 20, 60))])
def connect_account(project_id: int, body: AccountIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    """Подключение по токену официального API. Пароли внешних платформ не принимаются и не хранятся."""
    with ctx.db.session() as s:
        a = ctx.accounts.connect(s, actor, project_id, platform=body.platform, token=body.token, external_id=body.external_id, display_name=body.display_name, mode=body.mode, sandbox=body.sandbox)
        return ctx.accounts.to_dict(a)


@router.post("/p/{project_id}/accounts/{account_id}/recheck")
def recheck(project_id: int, account_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.ACCOUNTS_MANAGE)
    with ctx.db.session() as s:
        return ctx.accounts.to_dict(ctx.accounts.recheck(s, actor, account_id))


@router.post("/p/{project_id}/accounts/{account_id}/reauthorize")
def reauthorize(project_id: int, account_id: int, body: TokenIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.accounts.to_dict(ctx.accounts.reauthorize(s, actor, project_id, account_id, body.token))


@router.post("/p/{project_id}/accounts/{account_id}/revoke")
def revoke(project_id: int, account_id: int, body: ReasonIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.accounts.to_dict(ctx.accounts.revoke(s, actor, project_id, account_id, body.reason))


@router.put("/p/{project_id}/accounts/{account_id}/mode")
def account_mode(project_id: int, account_id: int, body: ModeIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.accounts.to_dict(ctx.accounts.set_mode(s, actor, project_id, account_id, body.mode))


@router.get("/p/{project_id}/accounts/oauth/meta/start")
def meta_oauth_start(project_id: int, request: Request, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    from ...publishing.accounts import MetaOAuth

    require(actor, Perm.ACCOUNTS_MANAGE)
    oauth = MetaOAuth(ctx, ctx.accounts.http)
    redirect = f"{ctx.settings.public_url.rstrip('/')}/api/p/{project_id}/accounts/oauth/meta/callback"
    return {"url": oauth.authorize_url(redirect, oauth.make_state(project_id, int(actor.id))), "redirect_uri": redirect}


@router.get("/p/{project_id}/accounts/oauth/meta/callback")
def meta_oauth_callback(project_id: int, code: str = "", state: str = "", actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    """Завершение OAuth Meta: проверяется подписанный state; страницы/IG-аккаунты возвращаются для выбора, токены в ответ не попадают."""
    from ...publishing.accounts import MetaOAuth

    require(actor, Perm.ACCOUNTS_MANAGE)
    oauth = MetaOAuth(ctx, ctx.accounts.http)
    pid, uid = oauth.check_state(state)
    if pid != project_id or str(uid) != actor.id:
        raise ValidationFailed("State OAuth не соответствует пользователю/проекту")
    redirect = f"{ctx.settings.public_url.rstrip('/')}/api/p/{project_id}/accounts/oauth/meta/callback"
    pages = oauth.exchange(code, redirect)
    connected = []
    with ctx.db.session() as s:
        for pg in pages:
            a = ctx.accounts.connect(s, actor, project_id, platform="facebook", token=pg["access_token"], external_id=pg["id"], display_name=pg.get("name", ""), mode="manual")
            connected.append(ctx.accounts.to_dict(a))
            ig = pg.get("instagram_business_account")
            if ig:
                a2 = ctx.accounts.connect(s, actor, project_id, platform="instagram", token=pg["access_token"], external_id=ig["id"], display_name=ig.get("username", ""), mode="manual")
                connected.append(ctx.accounts.to_dict(a2))
    return {"connected": connected}


# --------------------------------------------------------------------- автопилот и выключатель
@router.get("/p/{project_id}/autopilot")
def autopilot(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    from ...db.models import Project
    from ...settings_model import load_project_settings

    with ctx.db.read() as s:
        cfg = load_project_settings(s.get(Project, project_id).settings)
        return {
            "mode": cfg.mode.value, "settings": cfg.autopilot.model_dump(), "llm_enabled": ctx.llm.enabled,
            "policies": [{"id": p.id, "platform": p.platform, "category": p.category, "enabled": p.enabled, "constraints": p.constraints, "updated_by": p.updated_by} for p in ctx.autopilot.policies(s, project_id)],
            "kill_switches": [{"id": k.id, "scope": k.scope_type, "value": k.scope_value, "reason": k.reason, "engaged_by": k.engaged_by, "engaged_at": k.engaged_at.isoformat()} for k in ctx.killswitch.active(s, project_id)],
        }  # fmt: skip


@router.put("/p/{project_id}/autopilot/mode")
def set_mode(project_id: int, body: ModeIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.autopilot.set_mode(s, actor, project_id, body.mode)


@router.put("/p/{project_id}/autopilot/policy")
def set_policy(project_id: int, body: PolicyIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        p = ctx.autopilot.set_policy(s, actor, project_id, enabled=body.enabled, platform=body.platform, category=body.category, constraints=body.constraints)
        return {"id": p.id, "enabled": p.enabled}


@router.post("/p/{project_id}/autopilot/tick", dependencies=[Depends(limit("tick", 6, 60))])
def tick(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AUTOPILOT_MANAGE if actor.type == "user" else Perm.PUBLISH_AUTO)
    return ctx.autopilot.tick(project_id)


@router.post("/p/{project_id}/killswitch")
def engage(project_id: int, body: KillIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    """«ОСТАНОВИТЬ АВТОПИЛОТ»: доступно пользователю и админу, действует сразу."""
    with ctx.db.session() as s:
        ks = ctx.killswitch.engage(s, actor, project_id=project_id, scope_type=body.scope_type, scope_value=body.scope_value, reason=body.reason)
        return {"id": ks.id, "scope": ks.scope_type, "value": ks.scope_value}


@router.post("/p/{project_id}/killswitch/{ks_id}/release")
def release(project_id: int, ks_id: int, body: NoteIn, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    """Снятие остановки — только явное действие администратора (право kill.release)."""
    with ctx.db.session() as s:
        ks = ctx.killswitch.release(s, actor, ks_id, body.note)
        return {"id": ks.id, "released_by": ks.released_by}


# -------------------------------------------------------------------------------- профиль
@router.get("/p/{project_id}/profile")
def profile(project_id: int, actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.AGENDA_VIEW)
    with ctx.db.read() as s:
        return ctx.profile.snapshot(s, project_id)


@router.put("/p/{project_id}/profile/settings")
def profile_settings(project_id: int, body: dict[str, Any], actor: Actor = Depends(project_actor), ctx: Container = Depends(get_ctx)):
    require(actor, Perm.PROFILE_EDIT)
    with ctx.db.session() as s:
        return ctx.profile.update_settings(s, project_id, actor, body)


__all__ = ["router", "client_ip", "urlencode"]
