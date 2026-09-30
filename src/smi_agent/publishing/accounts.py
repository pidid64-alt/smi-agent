"""Подключение аккаунтов платформ (ТЗ §24, §36–38): только токены/OAuth через официальные API. Пароли внешних платформ не запрашиваются и не хранятся."""

from __future__ import annotations

import hashlib
import hmac
import secrets as pysecrets
import time
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import AccountStatus, Platform, PublishMode, PubState
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..db.models import Notification, PlatformAccount, Publication
from ..security.rbac import Actor, Perm, authorize
from .adapters.base import PlatformHttp, PublisherAdapter
from .adapters.meta import FacebookAdapter, InstagramAdapter
from .adapters.sandbox import SandboxAdapter
from .adapters.telegram import TelegramAdapter

META_SCOPES = ["pages_show_list", "pages_manage_posts", "pages_read_engagement", "instagram_basic", "instagram_content_publish", "instagram_manage_insights", "business_management"]


class AccountService:
    def __init__(self, ctx: Any, http: PlatformHttp | None = None):
        self.ctx = ctx
        self.http = http or PlatformHttp(ctx.settings)

    # ---------------------------------------------------------------- адаптеры
    def adapter_for(self, account: PlatformAccount) -> PublisherAdapter:
        st = self.ctx.settings
        if (account.settings or {}).get("sandbox"):
            return SandboxAdapter(account.platform)
        if account.platform == Platform.TELEGRAM.value:
            return TelegramAdapter(self.http, st.telegram_api_base)
        if account.platform == Platform.INSTAGRAM.value:
            return InstagramAdapter(self.http, st.graph_api_base, st.meta_graph_version)
        if account.platform == Platform.FACEBOOK.value:
            return FacebookAdapter(self.http, st.graph_api_base, st.meta_graph_version, self.ctx.clock)
        raise ValidationFailed(f"Платформа {account.platform} не поддерживается (первый этап: Telegram, Instagram, Facebook)")

    def token_for(self, s: Session, actor: Actor, account: PlatformAccount, purpose: str = "publish") -> str:
        if account.status == AccountStatus.REVOKED.value:
            raise Conflict("Доступ аккаунта отозван", code="account_revoked")
        if account.secret_id is None:
            return ""
        return self.ctx.secrets.get(s, actor, account.secret_id, purpose=purpose)

    # ------------------------------------------------------------------ CRUD
    def connect(self, s: Session, actor: Actor, project_id: int, *, platform: str, token: str = "", external_id: str = "", display_name: str = "", mode: str = "manual", sandbox: bool = False) -> PlatformAccount:
        authorize(actor, Perm.ACCOUNTS_MANAGE)
        try:
            Platform(platform)
        except ValueError as e:
            raise ValidationFailed("Поддерживаются только Telegram, Instagram и Facebook (первый этап)") from e
        if mode not in {m.value for m in PublishMode}:
            raise ValidationFailed("Режим публикации: manual | scheduled | auto")
        if mode == PublishMode.AUTO.value and actor.role != "admin" and actor.type == "user":
            raise ValidationFailed("Автоматический режим включает администратор")
        if sandbox and self.ctx.settings.is_production and not self.ctx.settings.demo_mode:
            raise ValidationFailed("Песочница недоступна в production")
        if not sandbox and (not token or not external_id):
            raise ValidationFailed("Укажите токен доступа и идентификатор канала/страницы/аккаунта")
        if token and any(x in token.lower() for x in ("password", "пароль")):
            raise ValidationFailed("Пароли внешних платформ не принимаются — используйте токен API")
        acc = PlatformAccount(project_id=project_id, platform=platform, display_name=display_name or f"{platform} {external_id}", external_id=external_id or "sandbox", status=AccountStatus.PENDING.value, mode=mode, settings={"sandbox": True} if sandbox else {}, connected_by=actor.label)
        s.add(acc)
        s.flush()
        if token:
            rec = self.ctx.secrets.put(s, actor, project_id, f"account:{acc.id}:{platform}", token, kind="platform_token")
            acc.secret_id = rec.id
        self._verify(s, actor, acc, token)
        self.ctx.audit.log(s, actor, "account.connect", project_id=project_id, target_type="account", target_id=acc.id, details={"platform": platform, "external_id": acc.external_id, "mode": mode, "status": acc.status, "sandbox": sandbox})
        return acc

    def _verify(self, s: Session, actor: Actor, acc: PlatformAccount, token: str) -> None:
        adapter = self.adapter_for(acc)
        chk = adapter.check_account(acc.external_id, token)
        acc.last_checked_at = self.ctx.clock.now()
        if chk.ok:
            acc.status, acc.last_error = AccountStatus.CONNECTED.value, ""
            acc.display_name = acc.display_name if acc.display_name and not acc.display_name.startswith(acc.platform + " ") else (chk.display_name or acc.display_name)
            acc.handle = chk.handle or acc.handle
            acc.scopes = chk.scopes
            acc.connected_at = acc.connected_at or self.ctx.clock.now()
        else:
            acc.status = AccountStatus.NEEDS_REAUTH.value if chk.reauth else AccountStatus.ERROR.value
            acc.last_error = chk.detail[:500]
        s.flush()

    def recheck(self, s: Session, actor: Actor, account_id: int) -> PlatformAccount:
        acc = self.get(s, actor.project_id, account_id) if actor.project_id else s.get(PlatformAccount, account_id)
        if acc is None:
            raise NotFound("Аккаунт не найден")
        if acc.status == AccountStatus.REVOKED.value:
            raise Conflict("Доступ отозван", code="account_revoked")
        token = self.token_for(s, actor, acc, purpose="check")
        self._verify(s, actor, acc, token)
        self.ctx.audit.log(s, actor, "account.recheck", project_id=acc.project_id, target_type="account", target_id=acc.id, outcome="ok" if acc.status == "connected" else "warn", details={"status": acc.status})
        return acc

    def reauthorize(self, s: Session, actor: Actor, project_id: int, account_id: int, token: str) -> PlatformAccount:
        authorize(actor, Perm.ACCOUNTS_MANAGE)
        acc = self.get(s, project_id, account_id)
        if acc.status == AccountStatus.REVOKED.value:
            raise Conflict("Аккаунт отозван — подключите его заново", code="account_revoked")
        rec = self.ctx.secrets.put(s, actor, project_id, f"account:{acc.id}:{acc.platform}", token, kind="platform_token")
        acc.secret_id = rec.id
        self._verify(s, actor, acc, token)
        self.ctx.audit.log(s, actor, "account.reauthorize", project_id=project_id, target_type="account", target_id=acc.id, details={"status": acc.status})
        return acc

    def revoke(self, s: Session, actor: Actor, project_id: int, account_id: int, reason: str = "") -> PlatformAccount:
        """Отзыв доступа (ТЗ §38): токен уничтожается, публикации в очереди снимаются, действие пишется в аудит."""
        authorize(actor, Perm.ACCOUNTS_MANAGE)
        acc = self.get(s, project_id, account_id)
        if acc.secret_id is not None:
            self.ctx.secrets.revoke(s, actor, acc.secret_id, reason=reason or "revoked account")
        acc.status, acc.revoked_at, acc.revoked_by = AccountStatus.REVOKED.value, self.ctx.clock.now(), actor.label
        held = 0
        for pub in s.scalars(select(Publication).where(Publication.account_id == acc.id, Publication.state.in_([PubState.SCHEDULED.value, PubState.AWAITING_APPROVAL.value, PubState.DRAFT.value]))):
            self.ctx.publishing.transition(s, pub, PubState.CANCELLED, actor, "Доступ к аккаунту отозван")
            held += 1
        s.add(Notification(project_id=project_id, level="warning", kind="account_revoked", title=f"Доступ к аккаунту отозван: {acc.display_name}", body=f"Снято публикаций: {held}. Причина: {reason or '—'}", created_at=self.ctx.clock.now()))
        self.ctx.audit.log(s, actor, "account.revoke", project_id=project_id, target_type="account", target_id=acc.id, details={"reason": reason, "cancelled": held})
        return acc

    def set_mode(self, s: Session, actor: Actor, project_id: int, account_id: int, mode: str) -> PlatformAccount:
        authorize(actor, Perm.ACCOUNTS_MANAGE)
        if mode not in {m.value for m in PublishMode}:
            raise ValidationFailed("Режим публикации: manual | scheduled | auto")
        acc = self.get(s, project_id, account_id)
        if mode == PublishMode.AUTO.value:
            authorize(actor, Perm.AUTOPILOT_MANAGE)
        old, acc.mode = acc.mode, mode
        self.ctx.audit.log(s, actor, "account.mode", project_id=project_id, target_type="account", target_id=acc.id, details={"from": old, "to": mode})
        return acc

    def get(self, s: Session, project_id: int | None, account_id: int) -> PlatformAccount:
        acc = s.get(PlatformAccount, account_id)
        if acc is None or (project_id is not None and acc.project_id != project_id):
            raise NotFound("Аккаунт не найден")
        return acc

    def list(self, s: Session, project_id: int) -> list[PlatformAccount]:
        return list(s.scalars(select(PlatformAccount).where(PlatformAccount.project_id == project_id).order_by(PlatformAccount.platform, PlatformAccount.id)))

    def to_dict(self, acc: PlatformAccount) -> dict[str, Any]:
        return {
            "id": acc.id, "platform": acc.platform, "display_name": acc.display_name, "external_id": acc.external_id, "handle": acc.handle, "status": acc.status, "mode": acc.mode,
            "sandbox": bool((acc.settings or {}).get("sandbox")), "scopes": acc.scopes, "connected_at": acc.connected_at.isoformat() if acc.connected_at else None,
            "last_checked_at": acc.last_checked_at.isoformat() if acc.last_checked_at else None, "last_error": acc.last_error, "has_token": acc.secret_id is not None,
        }  # fmt: skip


class MetaOAuth:
    """OAuth Meta (Facebook Login): подписанный state против CSRF, обмен кода, долгоживущий токен, список страниц. Проверено только на моках."""

    def __init__(self, ctx: Any, http: PlatformHttp):
        self.ctx, self.http = ctx, http

    def _key(self) -> bytes:
        return hashlib.sha256((self.ctx.settings.master_keys.get_secret_value() or "dev").encode()).digest()

    def make_state(self, project_id: int, user_id: int) -> str:
        nonce = pysecrets.token_urlsafe(12)
        payload = f"{project_id}.{user_id}.{int(time.time())}.{nonce}"
        sig = hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{payload}.{sig}"

    def check_state(self, state: str, max_age_s: int = 600) -> tuple[int, int]:
        try:
            project_id, user_id, ts, nonce, sig = state.split(".")
            payload = f"{project_id}.{user_id}.{ts}.{nonce}"
            ok = hmac.compare_digest(sig, hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()[:32])
            if not ok or time.time() - int(ts) > max_age_s:
                raise ValueError
            return int(project_id), int(user_id)
        except ValueError as e:
            raise ValidationFailed("Недействительный state OAuth (возможна подделка запроса)") from e

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        st = self.ctx.settings
        if not st.meta_app_id:
            raise ValidationFailed("Не настроен SMI_META_APP_ID")
        q = urlencode({"client_id": st.meta_app_id, "redirect_uri": redirect_uri, "state": state, "scope": ",".join(META_SCOPES), "response_type": "code"})
        return f"https://www.facebook.com/{st.meta_graph_version}/dialog/oauth?{q}"

    def exchange(self, code: str, redirect_uri: str) -> list[dict[str, Any]]:
        st = self.ctx.settings
        base = f"{st.graph_api_base.rstrip('/')}/{st.meta_graph_version}"
        secret = st.meta_app_secret.get_secret_value()
        r = self.http.request("GET", f"{base}/oauth/access_token", params={"client_id": st.meta_app_id, "client_secret": secret, "redirect_uri": redirect_uri, "code": code})
        if r.status_code >= 400:
            raise ValidationFailed("Meta отклонила код авторизации")
        short = r.json()["access_token"]
        r2 = self.http.request("GET", f"{base}/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": st.meta_app_id, "client_secret": secret, "fb_exchange_token": short})
        long_tok = r2.json().get("access_token", short) if r2.status_code < 400 else short
        r3 = self.http.request("GET", f"{base}/me/accounts", token=long_tok, params={"fields": "id,name,access_token,instagram_business_account{id,username}"})
        if r3.status_code >= 400:
            raise ValidationFailed("Не удалось получить список страниц")
        return r3.json().get("data", [])
