from __future__ import annotations


class AppError(Exception):
    """Базовая ошибка приложения; code — машинное имя, message — текст для пользователя (рус.)."""

    status = 400
    code = "app_error"

    def __init__(self, message: str = "", *, code: str | None = None, details: dict | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        self.details = details or {}


class NotFound(AppError):
    status = 404
    code = "not_found"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class Unauthorized(AppError):
    status = 401
    code = "unauthorized"


class Conflict(AppError):
    status = 409
    code = "conflict"


class ValidationFailed(AppError):
    status = 422
    code = "validation_failed"


class PolicyBlocked(AppError):
    """Операция запрещена политикой (kill-switch, режим публикации, проверки)."""

    status = 423
    code = "policy_blocked"


class SSRFBlocked(AppError):
    status = 400
    code = "ssrf_blocked"


class FetchError(AppError):
    status = 502
    code = "fetch_error"
