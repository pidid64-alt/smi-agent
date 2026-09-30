"""Аутентификация и сессии (ТЗ §36, §55): scrypt, TOTP-MFA, блокировка подбора, сессии с CSRF-токеном, сервисные токены.

Принципы: пароль и токены не попадают в логи/аудит; в БД хранятся только хеши токенов; сообщения об ошибках входа не раскрывают,
существует ли пользователь; повтор TOTP-кода отклоняется; админ в production обязан включить MFA.
"""

from __future__ import annotations

import hmac
import logging
import secrets as pysecrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..core.enums import Role
from ..core.errors import Conflict, Forbidden, NotFound, Unauthorized, ValidationFailed
from ..db.models import ApiToken, AuthSession, LoginAttempt, Membership, Project, User
from .passwords import check_password_policy, hash_password, needs_rehash, sha256_token, verify_password
from .rbac import Actor, Perm, authorize
from .totp import generate_secret, provisioning_uri, verify_totp

log = logging.getLogger(__name__)
GENERIC_LOGIN_ERROR = "Неверные учётные данные или учётная запись временно заблокирована"
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    return _DUMMY_HASH


@dataclass
class Principal:
    user_id: int
    username: str
    is_superadmin: bool
    kind: str  # human | service
    via: str  # session | token
    session_id: int | None = None
    csrf: str | None = None
    token_project_id: int | None = None
    mfa_ok: bool = True  # False — сессия ограничена: нужно включить MFA
    ip: str = ""


@dataclass
class LoginResult:
    token: str
    csrf: str
    user: dict[str, Any]
    mfa_setup_required: bool


class AuthService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------ пользователи
    def _admin_anywhere(self, s: Session, user: User) -> bool:
        if user.is_superadmin:
            return True
        return s.scalar(select(func.count()).select_from(Membership).where(Membership.user_id == user.id, Membership.role == Role.ADMIN.value)) > 0

    def create_user(self, s: Session, actor: Actor | None, *, username: str, password: str, display_name: str = "", project_id: int | None = None, role: str | None = None, superadmin: bool = False, kind: str = "human") -> User:
        if actor is not None:
            authorize(actor, Perm.USERS_MANAGE)
        username = username.strip().lower()
        if not username or len(username) > 80 or not all(c.isalnum() or c in "._-@" for c in username):
            raise ValidationFailed("Имя пользователя: буквы, цифры и . _ - @")
        if s.scalar(select(User.id).where(User.username == username)):
            raise Conflict("Пользователь с таким именем уже существует")
        if kind == "human":
            problems = check_password_policy(password, production=self.ctx.settings.is_production, username=username)
            if problems:
                raise ValidationFailed("; ".join(problems))
        if role is not None and role not in {r.value for r in Role}:
            raise ValidationFailed("Недопустимая роль")
        if superadmin and actor is not None and not actor.role == Role.ADMIN.value:
            raise Forbidden("Суперадминистратора создаёт только администратор")
        u = User(username=username, display_name=display_name or username, kind=kind, password_hash=hash_password(password) if kind == "human" else "", is_superadmin=superadmin, created_at=self.ctx.clock.now(), password_changed_at=self.ctx.clock.now())
        s.add(u)
        s.flush()
        if project_id is not None and role:
            s.add(Membership(user_id=u.id, project_id=project_id, role=role))
        self.ctx.audit.log(s, actor or Actor.system(), "user.create", project_id=project_id, target_type="user", target_id=u.id, details={"username": username, "role": role, "superadmin": superadmin, "kind": kind})
        return u

    def set_membership(self, s: Session, actor: Actor, project_id: int, user_id: int, role: str | None) -> None:
        authorize(actor, Perm.USERS_MANAGE)
        if role is not None and role not in {r.value for r in Role}:
            raise ValidationFailed("Недопустимая роль")
        if s.get(Project, project_id) is None or s.get(User, user_id) is None:
            raise NotFound("Проект или пользователь не найден")
        m = s.scalars(select(Membership).where(Membership.user_id == user_id, Membership.project_id == project_id)).first()
        old = m.role if m else None
        if role is None and m is not None:
            s.delete(m)
        elif m is None and role is not None:
            s.add(Membership(user_id=user_id, project_id=project_id, role=role))
        elif m is not None:
            m.role = role
        self.revoke_sessions(s, user_id)  # изменение прав — повод перевыпустить сессии
        self.ctx.audit.log(s, actor, "user.role", project_id=project_id, target_type="user", target_id=user_id, details={"from": old, "to": role})

    def memberships(self, s: Session, user_id: int) -> list[dict[str, Any]]:
        u = s.get(User, user_id)
        q = select(Project).where(Project.archived_at.is_(None)).order_by(Project.id)
        out = []
        roles = {m.project_id: m.role for m in s.scalars(select(Membership).where(Membership.user_id == user_id))}
        for p in s.scalars(q):
            if u.is_superadmin:
                out.append({"project_id": p.id, "slug": p.slug, "name": p.name, "role": Role.ADMIN.value})
            elif p.id in roles:
                out.append({"project_id": p.id, "slug": p.slug, "name": p.name, "role": roles[p.id]})
        return out

    def role_in(self, s: Session, principal: Principal, project_id: int) -> str | None:
        if principal.token_project_id is not None:
            return Role.AI_SERVICE.value if principal.token_project_id == project_id else None
        if principal.is_superadmin:
            return Role.ADMIN.value
        m = s.scalars(select(Membership).where(Membership.user_id == principal.user_id, Membership.project_id == project_id)).first()
        return m.role if m else None

    def actor_for(self, s: Session, principal: Principal, project_id: int) -> Actor:
        role = self.role_in(s, principal, project_id)
        if role is None:
            raise Forbidden("Нет доступа к проекту")
        if principal.kind == "service":
            return Actor("service", str(principal.user_id), role, principal.ip, project_id)
        return Actor.user(principal.user_id, role, principal.ip, project_id)

    # ------------------------------------------------------------------------ вход
    def _failures(self, s: Session, key: str, since) -> int:
        return s.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == key, LoginAttempt.success.is_(False), LoginAttempt.ts >= since)) or 0

    def login(self, username: str, password: str, *, totp: str = "", ip: str = "", user_agent: str = "") -> LoginResult:
        st = self.ctx.settings
        now = self.ctx.clock.now()
        uname = (username or "").strip().lower()[:80]
        window = now - timedelta(minutes=st.login_lock_minutes)
        ip_key, user_key = f"ip:{ip}", f"user:{uname}"
        with self.ctx.db.session() as s:
            locked = self._failures(s, ip_key, window) >= st.login_max_failures * 4 or self._failures(s, user_key, window) >= st.login_max_failures
            u = s.scalars(select(User).where(User.username == uname)).first()
            ok = False
            reason = ""
            if locked:
                reason = "locked"
                verify_password(password or "", _dummy_hash())  # выравниваем время ответа
            elif u is None or not u.is_active or u.kind != "human" or not u.password_hash:
                verify_password(password or "", _dummy_hash())
                reason = "unknown_or_disabled"
            elif not verify_password(password or "", u.password_hash):
                reason = "bad_password"
            elif u.mfa_enabled:
                secret = self.ctx.secrets.get(s, Actor.system(), u.mfa_secret_id, purpose="mfa-login") if u.mfa_secret_id else ""
                step = verify_totp(secret, totp, last_step=u.mfa_last_step) if secret else None
                if step is None:
                    reason = "bad_totp"
                else:
                    u.mfa_last_step = step
                    ok = True
            else:
                ok = True
            s.add(LoginAttempt(key=user_key, ts=now, success=ok))
            s.add(LoginAttempt(key=ip_key, ts=now, success=ok))
            if not ok:
                self.ctx.audit.log(s, Actor("user", u.username if u else uname or "?", "", ip), "auth.login_failed", outcome="denied", details={"reason": reason, "user_agent": user_agent[:120]})
                s.commit()  # фиксируем попытку и аудит до выброса исключения
                raise Unauthorized(GENERIC_LOGIN_ERROR, code="login_failed")
            if needs_rehash(u.password_hash):
                u.password_hash = hash_password(password)
            u.last_login_at = now
            u.failed_logins = 0
            token = pysecrets.token_urlsafe(32)
            csrf = pysecrets.token_urlsafe(24)
            s.add(AuthSession(token_hash=sha256_token(token), user_id=u.id, csrf_token=csrf, created_at=now, last_seen_at=now, expires_at=now + timedelta(hours=st.session_absolute_hours), ip=ip[:64], user_agent=user_agent[:300]))
            mfa_setup = st.mfa_required_for_admin and self._admin_anywhere(s, u) and not u.mfa_enabled
            self.ctx.audit.log(s, Actor.user(u.id, "", ip), "auth.login", details={"mfa": u.mfa_enabled, "mfa_setup_required": mfa_setup})
            return LoginResult(token, csrf, self.user_dict(s, u), mfa_setup)

    def user_dict(self, s: Session, u: User) -> dict[str, Any]:
        return {"id": u.id, "username": u.username, "display_name": u.display_name, "is_superadmin": u.is_superadmin, "mfa_enabled": u.mfa_enabled, "projects": self.memberships(s, u.id)}

    # -------------------------------------------------------------------- сессии
    def authenticate_session(self, token: str, *, ip: str = "") -> Principal | None:
        if not token:
            return None
        st = self.ctx.settings
        now = self.ctx.clock.now()
        with self.ctx.db.session() as s:
            row = s.scalars(select(AuthSession).where(AuthSession.token_hash == sha256_token(token))).first()
            if row is None or row.revoked_at is not None or row.expires_at <= now or (now - row.last_seen_at) > timedelta(minutes=st.session_idle_minutes):
                if row is not None and row.revoked_at is None:
                    row.revoked_at = now
                return None
            u = s.get(User, row.user_id)
            if u is None or not u.is_active:
                return None
            if (now - row.last_seen_at).total_seconds() > 60:
                row.last_seen_at = now
            mfa_ok = not (st.mfa_required_for_admin and self._admin_anywhere(s, u) and not u.mfa_enabled)
            return Principal(u.id, u.username, u.is_superadmin, u.kind, "session", row.id, row.csrf_token, None, mfa_ok, ip)

    def authenticate_token(self, bearer: str, *, ip: str = "") -> Principal | None:
        if not bearer or not bearer.startswith("smi_"):
            return None
        now = self.ctx.clock.now()
        with self.ctx.db.session() as s:
            row = s.scalars(select(ApiToken).where(ApiToken.token_hash == sha256_token(bearer))).first()
            if row is None or row.revoked_at is not None or (row.expires_at and row.expires_at <= now):
                return None
            u = s.get(User, row.user_id)
            if u is None or not u.is_active or u.kind != "service":
                return None
            if row.last_used_at is None or (now - row.last_used_at).total_seconds() > 60:
                row.last_used_at = now
            return Principal(u.id, u.username, False, "service", "token", None, None, row.project_id, True, ip)

    def logout(self, token: str, *, actor: Actor | None = None) -> None:
        with self.ctx.db.session() as s:
            row = s.scalars(select(AuthSession).where(AuthSession.token_hash == sha256_token(token))).first()
            if row is not None and row.revoked_at is None:
                row.revoked_at = self.ctx.clock.now()
                self.ctx.audit.log(s, actor or Actor.user(row.user_id, ""), "auth.logout")

    def revoke_sessions(self, s: Session, user_id: int, *, except_id: int | None = None) -> int:
        q = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        if except_id is not None:
            q = q.where(AuthSession.id != except_id)
        return s.execute(q.values(revoked_at=self.ctx.clock.now())).rowcount

    # ------------------------------------------------------------------------ MFA
    def mfa_begin(self, s: Session, user_id: int) -> dict[str, str]:
        u = s.get(User, user_id)
        if u.mfa_enabled:
            raise Conflict("MFA уже включена")
        secret = generate_secret()
        rec = self.ctx.secrets.put(s, Actor.system(), None, f"mfa:{u.id}", secret, kind="totp")
        u.mfa_secret_id = rec.id
        self.ctx.audit.log(s, Actor.user(u.id, ""), "auth.mfa_begin")
        return {"secret": secret, "uri": provisioning_uri(secret, u.username)}

    def mfa_confirm(self, s: Session, user_id: int, code: str) -> None:
        u = s.get(User, user_id)
        if u.mfa_enabled or not u.mfa_secret_id:
            raise Conflict("Сначала начните настройку MFA")
        secret = self.ctx.secrets.get(s, Actor.system(), u.mfa_secret_id, purpose="mfa-confirm")
        step = verify_totp(secret, code)
        if step is None:
            raise ValidationFailed("Неверный код. Проверьте время на устройстве и попробуйте ещё раз")
        u.mfa_enabled, u.mfa_last_step = True, step
        self.ctx.audit.log(s, Actor.user(u.id, ""), "auth.mfa_enabled")

    def mfa_disable(self, s: Session, user_id: int, password: str, code: str) -> None:
        u = s.get(User, user_id)
        if not u.mfa_enabled:
            return
        if not verify_password(password, u.password_hash):
            raise Unauthorized("Неверный пароль", code="bad_password")
        secret = self.ctx.secrets.get(s, Actor.system(), u.mfa_secret_id, purpose="mfa-disable")
        if verify_totp(secret, code, last_step=u.mfa_last_step) is None:
            raise Unauthorized("Неверный код MFA", code="bad_totp")
        if self.ctx.settings.mfa_required_for_admin and self._admin_anywhere(s, u):
            raise Forbidden("Для администратора MFA отключить нельзя (политика безопасности)")
        u.mfa_enabled = False
        self.ctx.secrets.revoke(s, Actor.user(u.id, ""), u.mfa_secret_id, reason="mfa disabled")
        u.mfa_secret_id = None
        self.ctx.audit.log(s, Actor.user(u.id, ""), "auth.mfa_disabled")

    # ---------------------------------------------------------------------- пароль
    def change_password(self, s: Session, user_id: int, old: str, new: str, *, keep_session_id: int | None = None) -> None:
        u = s.get(User, user_id)
        if not verify_password(old, u.password_hash):
            raise Unauthorized("Неверный текущий пароль", code="bad_password")
        problems = check_password_policy(new, production=self.ctx.settings.is_production, username=u.username)
        if problems:
            raise ValidationFailed("; ".join(problems))
        if verify_password(new, u.password_hash):
            raise ValidationFailed("Новый пароль должен отличаться от текущего")
        u.password_hash, u.password_changed_at = hash_password(new), self.ctx.clock.now()
        n = self.revoke_sessions(s, u.id, except_id=keep_session_id)
        self.ctx.audit.log(s, Actor.user(u.id, ""), "auth.password_changed", details={"sessions_revoked": n})

    # ---------------------------------------------------------------- сервисные токены
    def create_api_token(self, s: Session, actor: Actor, project_id: int, name: str, *, expires_days: int | None = 90) -> tuple[str, ApiToken]:
        authorize(actor, Perm.USERS_MANAGE)
        if not name.strip():
            raise ValidationFailed("Укажите название токена")
        svc = User(username=f"svc-{pysecrets.token_hex(4)}", display_name=name[:80], kind="service", password_hash="", created_at=self.ctx.clock.now())
        s.add(svc)
        s.flush()
        s.add(Membership(user_id=svc.id, project_id=project_id, role=Role.AI_SERVICE.value))
        prefix = pysecrets.token_hex(4)
        token = f"smi_{prefix}_{pysecrets.token_urlsafe(32)}"
        row = ApiToken(project_id=project_id, user_id=svc.id, name=name[:120], prefix=f"smi_{prefix}", token_hash=sha256_token(token), created_by=int(actor.id) if actor.id.isdigit() else None, created_at=self.ctx.clock.now(), expires_at=self.ctx.clock.now() + timedelta(days=expires_days) if expires_days else None)
        s.add(row)
        s.flush()
        self.ctx.audit.log(s, actor, "token.create", project_id=project_id, target_type="api_token", target_id=row.id, details={"name": name, "prefix": row.prefix, "expires_days": expires_days})
        return token, row

    def revoke_api_token(self, s: Session, actor: Actor, project_id: int, token_id: int) -> None:
        authorize(actor, Perm.USERS_MANAGE)
        row = s.get(ApiToken, token_id)
        if row is None or row.project_id != project_id:
            raise NotFound("Токен не найден")
        if row.revoked_at is None:
            row.revoked_at = self.ctx.clock.now()
            self.ctx.audit.log(s, actor, "token.revoke", project_id=project_id, target_type="api_token", target_id=row.id, details={"prefix": row.prefix})

    def list_api_tokens(self, s: Session, project_id: int) -> list[dict[str, Any]]:
        return [{"id": t.id, "name": t.name, "prefix": t.prefix, "created_at": t.created_at.isoformat(), "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None, "expires_at": t.expires_at.isoformat() if t.expires_at else None, "revoked": t.revoked_at is not None} for t in s.scalars(select(ApiToken).where(ApiToken.project_id == project_id).order_by(ApiToken.id.desc()))]

    # --------------------------------------------------------------------- CSRF
    @staticmethod
    def csrf_ok(expected: str | None, provided: str | None) -> bool:
        return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)
