from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class Role(StrEnum):
    USER = "user"
    ADMIN = "admin"
    AUDITOR = "auditor"
    AI_SERVICE = "ai_service"


class AgentMode(StrEnum):
    LEARNING = "learning"  # Режим 1 — ОБУЧЕНИЕ
    CO_EDITOR = "co_editor"  # Режим 2 — СО-РЕДАКТОР
    AUTOPILOT = "autopilot"  # Режим 3 — АВТОПИЛОТ


class PublishMode(StrEnum):
    MANUAL = "manual"  # Режим 1 — ручное подтверждение
    SCHEDULED = "scheduled"  # Режим 2 — по расписанию
    AUTO = "auto"  # Режим 3 — автоматическая публикация


class PubState(StrEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_REVIEW = "needs_review"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    ERROR = "error"
    CANCELLED = "cancelled"


class AttemptOutcome(StrEnum):
    STARTED = "started"
    SUCCESS = "success"
    NOT_SENT = "not_sent"  # гарантированно не дошло до платформы
    RATE_LIMITED = "rate_limited"
    FAILED_PERMANENT = "failed_permanent"
    UNKNOWN = "unknown"  # исход неизвестен: повторная отправка запрещена до сверки


class VerificationStatus(StrEnum):
    CONFIRMED = "confirmed"  # подтверждено (первичный источник)
    MULTI_CONFIRMED = "multi_confirmed"  # подтверждено несколькими источниками
    NEEDS_CHECK = "needs_check"  # требуется дополнительная проверка
    REJECTED = "rejected"


class Geo(StrEnum):
    KZ = "kz"
    CA = "ca"
    WORLD = "world"


class Phase(StrEnum):
    EMERGING = "emerging"
    RISING = "rising"
    PEAK = "peak"
    FADING = "fading"
    STALE = "stale"


class Relation(StrEnum):
    ORIGINAL = "original"
    DUPLICATE = "duplicate"
    REPRINT = "reprint"
    TRANSLATION = "translation"
    UPDATE = "update"
    INDEPENDENT = "independent"


class ActionKind(StrEnum):
    SELECT = "select"
    REJECT = "reject"
    REPLACE = "replace"
    MORE_INFO = "more_info"
    CHANGE_ANGLE = "change_angle"
    CHANGE_FORMAT = "change_format"
    MODIFY = "modify"  # выбрал, но изменил (акцент/язык/гео)


class KillScope(StrEnum):
    SYSTEM = "system"
    PROJECT = "project"
    ACCOUNT = "account"
    PLATFORM = "platform"
    CATEGORY = "category"


class AccountStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"
    REVOKED = "revoked"


PUB_STATE_LABELS = {
    PubState.DRAFT: "Черновик",
    PubState.AWAITING_APPROVAL: "Ожидает подтверждения",
    PubState.NEEDS_REVIEW: "Требует проверки",
    PubState.SCHEDULED: "Запланировано",
    PubState.PUBLISHING: "Публикуется",
    PubState.PUBLISHED: "Опубликовано",
    PubState.ERROR: "Ошибка",
    PubState.CANCELLED: "Отменено",
}

VERIFICATION_LABELS = {
    VerificationStatus.CONFIRMED: "подтверждено",
    VerificationStatus.MULTI_CONFIRMED: "подтверждено несколькими источниками",
    VerificationStatus.NEEDS_CHECK: "требуется дополнительная проверка",
    VerificationStatus.REJECTED: "не подтверждено",
}
