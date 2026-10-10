from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ...container import Container
from ...core.errors import Forbidden
from ...security.auth import Principal
from ..deps import COOKIE, _origin_ok, client_ip, get_ctx, get_principal, limit

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)
    totp: str = Field(default="", max_length=10)


class PasswordIn(BaseModel):
    old: str = Field(max_length=200)
    new: str = Field(max_length=200)


class CodeIn(BaseModel):
    code: str = Field(max_length=10)


class MfaDisableIn(BaseModel):
    password: str = Field(max_length=200)
    code: str = Field(max_length=10)


@router.post("/login", dependencies=[Depends(limit("login", 10, 60))])
def login(body: LoginIn, request: Request, response: Response, ctx: Container = Depends(get_ctx)):
    if not _origin_ok(request, ctx):
        raise Forbidden("Запрос отклонён защитой от CSRF", code="csrf")
    res = ctx.auth.login(body.username, body.password, totp=body.totp, ip=client_ip(request), user_agent=request.headers.get("user-agent", ""))
    response.set_cookie(COOKIE, res.token, httponly=True, samesite="strict", secure=ctx.settings.is_production, max_age=ctx.settings.session_absolute_hours * 3600, path="/")
    return {"user": res.user, "csrf": res.csrf, "mfa_setup_required": res.mfa_setup_required}


@router.post("/logout")
def logout(request: Request, response: Response, principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    ctx.auth.logout(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    from ...db.models import User

    with ctx.db.read() as s:
        u = s.get(User, principal.user_id)
        return {"user": ctx.auth.user_dict(s, u), "csrf": principal.csrf, "mfa_setup_required": not principal.mfa_ok, "via": principal.via, "demo": ctx.settings.demo_mode, "env": ctx.settings.env}


@router.post("/password", dependencies=[Depends(limit("password", 5, 60))])
def change_password(body: PasswordIn, principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        ctx.auth.change_password(s, principal.user_id, body.old, body.new, keep_session_id=principal.session_id)
    return {"ok": True}


@router.post("/mfa/begin")
def mfa_begin(principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        return ctx.auth.mfa_begin(s, principal.user_id)  # секрет и QR-ссылка показываются один раз


@router.post("/mfa/confirm", dependencies=[Depends(limit("mfa", 10, 60))])
def mfa_confirm(body: CodeIn, principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        ctx.auth.mfa_confirm(s, principal.user_id, body.code)
    return {"ok": True}


@router.post("/mfa/disable", dependencies=[Depends(limit("mfa", 10, 60))])
def mfa_disable(body: MfaDisableIn, principal: Principal = Depends(get_principal), ctx: Container = Depends(get_ctx)):
    with ctx.db.session() as s:
        ctx.auth.mfa_disable(s, principal.user_id, body.password, body.code)
    return {"ok": True}
