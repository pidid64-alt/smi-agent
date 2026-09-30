"""Роли и права (ТЗ §55): Пользователь, Администратор, Аудитор, AI-сервис."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..core.enums import Role
from ..core.errors import Forbidden


class Perm(StrEnum):
    AGENDA_VIEW = "agenda.view"
    PROPOSALS_VIEW = "proposals.view"
    PROPOSALS_ACT = "proposals.act"
    CONTENT_EDIT = "content.edit"
    PUBLISH_APPROVE = "publish.approve"
    PUBLISH_CANCEL = "publish.cancel"
    ANALYTICS_VIEW = "analytics.view"
    REPORTS_VIEW = "reports.view"
    PROFILE_EDIT = "profile.edit"
    SOURCES_MANAGE = "sources.manage"
    ACCOUNTS_MANAGE = "accounts.manage"
    POLICIES_MANAGE = "policies.manage"
    AUTOPILOT_MANAGE = "autopilot.manage"
    KILL_ENGAGE = "kill.engage"
    KILL_RELEASE = "kill.release"
    AUDIT_VIEW = "audit.view"
    AUDIT_EXPORT = "audit.export"
    USERS_MANAGE = "users.manage"
    SECRETS_MANAGE = "secrets.manage"
    BACKUPS_MANAGE = "backups.manage"
    SYSTEM_VIEW = "system.view"
    SETTINGS_EDIT = "settings.edit"
    # права AI-сервиса (машинный исполнитель)
    MONITOR_RUN = "monitor.run"
    FUNNEL_RUN = "funnel.run"
    CONTENT_PREPARE = "content.prepare"
    CHECK_RUN = "check.run"
    PUBLISH_AUTO = "publish.auto"
    METRICS_COLLECT = "metrics.collect"


_USER = {
    Perm.AGENDA_VIEW, Perm.PROPOSALS_VIEW, Perm.PROPOSALS_ACT, Perm.CONTENT_EDIT, Perm.PUBLISH_APPROVE,
    Perm.PUBLISH_CANCEL, Perm.ANALYTICS_VIEW, Perm.REPORTS_VIEW, Perm.PROFILE_EDIT, Perm.KILL_ENGAGE,
    Perm.FUNNEL_RUN, Perm.SYSTEM_VIEW,
}  # fmt: skip
_ADMIN = _USER | {
    Perm.SOURCES_MANAGE, Perm.ACCOUNTS_MANAGE, Perm.POLICIES_MANAGE, Perm.AUTOPILOT_MANAGE, Perm.KILL_RELEASE,
    Perm.AUDIT_VIEW, Perm.AUDIT_EXPORT, Perm.USERS_MANAGE, Perm.SECRETS_MANAGE, Perm.BACKUPS_MANAGE,
    Perm.SETTINGS_EDIT, Perm.MONITOR_RUN,
}  # fmt: skip
_AUDITOR = {Perm.AUDIT_VIEW, Perm.AUDIT_EXPORT, Perm.SYSTEM_VIEW, Perm.ANALYTICS_VIEW, Perm.REPORTS_VIEW}
_AI = {
    Perm.MONITOR_RUN, Perm.FUNNEL_RUN, Perm.CONTENT_PREPARE, Perm.CHECK_RUN, Perm.PUBLISH_AUTO,
    Perm.METRICS_COLLECT, Perm.AGENDA_VIEW, Perm.PROPOSALS_VIEW, Perm.ANALYTICS_VIEW, Perm.KILL_ENGAGE,
}  # fmt: skip

ROLE_PERMS: dict[Role, frozenset[Perm]] = {
    Role.USER: frozenset(_USER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.AUDITOR: frozenset(_AUDITOR),
    Role.AI_SERVICE: frozenset(_AI),
}


@dataclass(frozen=True)
class Actor:
    type: str  # user | service | system
    id: str
    role: str = ""
    ip: str = ""
    project_id: int | None = None

    @property
    def label(self) -> str:
        return f"{self.type}:{self.id}"

    @staticmethod
    def system() -> Actor:
        return Actor("system", "system", "")

    @staticmethod
    def ai(name: str = "autopilot", project_id: int | None = None) -> Actor:
        return Actor("service", name, Role.AI_SERVICE.value, project_id=project_id)

    @staticmethod
    def user(user_id: int | str, role: str, ip: str = "", project_id: int | None = None) -> Actor:
        return Actor("user", str(user_id), role, ip, project_id)


def can(actor: Actor, perm: Perm) -> bool:
    if actor.type == "system":
        return True
    try:
        return perm in ROLE_PERMS[Role(actor.role)]
    except (ValueError, KeyError):
        return False


def authorize(actor: Actor, perm: Perm) -> None:
    if not can(actor, perm):
        raise Forbidden("Недостаточно прав для этого действия", code="forbidden", details={"perm": perm.value})
