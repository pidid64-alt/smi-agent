"""Аварийный выключатель автопилота «ОСТАНОВИТЬ АВТОПИЛОТ» (ТЗ §39): уровни система / проект / аккаунт / платформа / категория.

Снятие — только явным действием пользователя с правом kill.release (администратор). Автоматического снятия по таймеру нет.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.enums import KillScope, PubState
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..db.models import Content, KillSwitch, Notification, Publication
from ..security.rbac import Actor, Perm, authorize


class KillSwitchService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def active(self, s: Session, project_id: int | None) -> list[KillSwitch]:
        q = select(KillSwitch).where(KillSwitch.released_at.is_(None))
        if project_id is not None:
            q = q.where(or_(KillSwitch.project_id == project_id, KillSwitch.scope_type == KillScope.SYSTEM.value))
        return list(s.scalars(q.order_by(KillSwitch.id)))

    def blocking(self, s: Session, project_id: int, *, platform: str | None = None, account_id: int | None = None, category: str | None = None) -> KillSwitch | None:
        for ks in self.active(s, project_id):
            t, v = ks.scope_type, ks.scope_value
            if t in (KillScope.SYSTEM.value, KillScope.PROJECT.value):
                return ks
            if t == KillScope.PLATFORM.value and platform and v == platform:
                return ks
            if t == KillScope.ACCOUNT.value and account_id is not None and v == str(account_id):
                return ks
            if t == KillScope.CATEGORY.value and category and v == category:
                return ks
        return None

    def engage(self, s: Session, actor: Actor, *, project_id: int | None, scope_type: str, scope_value: str = "", reason: str = "") -> KillSwitch:
        authorize(actor, Perm.KILL_ENGAGE)
        try:
            scope = KillScope(scope_type)
        except ValueError as e:
            raise ValidationFailed("Недопустимый уровень остановки") from e
        if scope != KillScope.SYSTEM and project_id is None:
            raise ValidationFailed("Не указан проект")
        if scope in (KillScope.ACCOUNT, KillScope.PLATFORM, KillScope.CATEGORY) and not scope_value:
            raise ValidationFailed("Укажите аккаунт / платформу / категорию")
        if scope == KillScope.SYSTEM and actor.type == "user" and actor.role != "admin":
            raise ValidationFailed("Остановка всей системы доступна администратору")
        pid = None if scope == KillScope.SYSTEM else project_id
        existing = s.scalars(select(KillSwitch).where(KillSwitch.released_at.is_(None), KillSwitch.scope_type == scope.value, KillSwitch.scope_value == scope_value, KillSwitch.project_id.is_(None) if pid is None else KillSwitch.project_id == pid)).first()
        if existing:
            return existing
        ks = KillSwitch(project_id=pid, scope_type=scope.value, scope_value=scope_value, engaged_at=self.ctx.clock.now(), engaged_by=actor.label, reason=reason[:500])
        s.add(ks)
        s.flush()
        held = self._hold_autonomous(s, ks, actor)
        s.add(Notification(project_id=pid, level="critical", kind="killswitch", title=f"Автопилот остановлен ({scope.value}{': ' + scope_value if scope_value else ''})", body=f"{reason or 'без указания причины'}. Приостановлено публикаций автопилота: {held}. Снять остановку может администратор.", payload={"kill_switch_id": ks.id}, created_at=self.ctx.clock.now()))
        self.ctx.audit.log(s, actor, "killswitch.engage", project_id=pid, target_type="kill_switch", target_id=ks.id, details={"scope": scope.value, "value": scope_value, "reason": reason, "held": held})
        return ks

    def _hold_autonomous(self, s: Session, ks: KillSwitch, actor: Actor) -> int:
        """Запланированные публикации автопилота в зоне действия переводятся в «Ожидает подтверждения»: после снятия они не выйдут «задним числом»."""
        q = select(Publication).join(Content, Content.id == Publication.content_pk).where(Publication.state == PubState.SCHEDULED.value, Publication.approved_by == "service:autopilot")
        if ks.scope_type != KillScope.SYSTEM.value:
            q = q.where(Publication.project_id == ks.project_id)
        if ks.scope_type == KillScope.PLATFORM.value:
            q = q.where(Publication.platform == ks.scope_value)
        elif ks.scope_type == KillScope.ACCOUNT.value:
            q = q.where(Publication.account_id == int(ks.scope_value or 0))
        elif ks.scope_type == KillScope.CATEGORY.value:
            q = q.where(Content.category == ks.scope_value)
        n = 0
        for pub in s.scalars(q):
            self.ctx.publishing.transition(s, pub, PubState.AWAITING_APPROVAL, actor, "Остановлено аварийным выключателем автопилота — требуется ручное подтверждение")
            pub.approved_by, pub.approved_at = None, None
            n += 1
        return n

    def release(self, s: Session, actor: Actor, ks_id: int, note: str = "") -> KillSwitch:
        authorize(actor, Perm.KILL_RELEASE)
        if actor.type != "user":
            raise Conflict("Остановку может снять только пользователь с правом kill.release — не автоматика", code="release_requires_human")
        ks = s.get(KillSwitch, ks_id)
        if ks is None:
            raise NotFound("Остановка не найдена")
        if ks.released_at is not None:
            return ks
        ks.released_at, ks.released_by, ks.release_note = self.ctx.clock.now(), actor.label, note[:500]
        s.flush()
        self.ctx.audit.log(s, actor, "killswitch.release", project_id=ks.project_id, target_type="kill_switch", target_id=ks.id, details={"scope": ks.scope_type, "value": ks.scope_value, "note": note})
        return ks
