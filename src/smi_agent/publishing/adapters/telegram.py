"""Telegram Bot API: sendMessage / sendPhoto / sendMediaGroup. Лимиты: текст 1–4096, подпись 0–1024, альбом 2–10."""

from __future__ import annotations

import json
from typing import Any

from ...core.enums import AttemptOutcome
from ..errors import PlatformError, classify_http
from .base import AccountCheck, PlatformHttp, PublishContext, PublishResult, ReconcileResult


class TelegramAdapter:
    platform = "telegram"

    def __init__(self, http: PlatformHttp, api_base: str = "https://api.telegram.org"):
        self.http, self.base = http, api_base.rstrip("/")

    def _call(self, token: str, method: str, *, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> dict[str, Any]:
        # токен бота — часть пути (так устроен Bot API): этот URL никогда не логируется; RedactingFilter маскирует его в любых логах
        r = self.http.request("POST", f"{self.base}/bot{token}/{method}", data=data, files=files)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200 and body.get("ok"):
            return body["result"]
        ra = (body.get("parameters") or {}).get("retry_after")
        desc = str(body.get("description", ""))[:300]
        code = f"tg_{body.get('error_code', r.status_code)}"
        err = classify_http(int(body.get("error_code", r.status_code)), body_excerpt=desc, retry_after=float(ra) if ra else None, platform_code=code)
        if "chat not found" in desc.lower() or "bot was kicked" in desc.lower() or "not enough rights" in desc.lower():
            err.reauth = True
        raise err

    @staticmethod
    def _result(res: dict[str, Any] | list[dict[str, Any]]) -> PublishResult:
        first = res[0] if isinstance(res, list) else res
        chat = first.get("chat", {})
        mid = first.get("message_id")
        username = chat.get("username")
        return PublishResult(external_id=f"{chat.get('id')}/{mid}", url=f"https://t.me/{username}/{mid}" if username else "", raw={"message_id": mid})

    def publish(self, ctx: PublishContext) -> PublishResult:
        chat = ctx.account_external_id
        common: dict[str, Any] = {"chat_id": chat}
        if ctx.parse_mode:
            common["parse_mode"] = ctx.parse_mode
        if ctx.fmt in ("photo",) and ctx.media:
            m = ctx.media[0]
            with m.path.open("rb") as f:
                res = self._call(ctx.token, "sendPhoto", data={**common, "caption": ctx.text}, files={"photo": (m.path.name, f.read(), m.mime)})
            return self._result(res)
        if ctx.fmt == "gallery" and len(ctx.media) >= 2:
            items, files = [], {}
            for i, m in enumerate(ctx.media[:10]):
                files[f"f{i}"] = (m.path.name, m.path.read_bytes(), m.mime)
                items.append({"type": "photo", "media": f"attach://f{i}", **({"caption": ctx.text, "parse_mode": ctx.parse_mode} if i == 0 and ctx.parse_mode else ({"caption": ctx.text} if i == 0 else {}))})
            res = self._call(ctx.token, "sendMediaGroup", data={"chat_id": chat, "media": json.dumps(items)}, files=files)
            return self._result(res)
        res = self._call(ctx.token, "sendMessage", data={**common, "text": ctx.text, "link_preview_options": json.dumps({"is_disabled": False})})
        return self._result(res)

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        # Bot API не позволяет читать историю канала и не поддерживает ключи идемпотентности — безопасно доказать «не отправлено» нельзя.
        return ReconcileResult("unknown", detail="Telegram Bot API не даёт проверить, вышло ли сообщение: проверьте канал вручную и подтвердите результат")

    def check_account(self, external_id: str, token: str) -> AccountCheck:
        try:
            me = self._call(token, "getMe")
            chat = self._call(token, "getChat", data={"chat_id": external_id})
            member = self._call(token, "getChatMember", data={"chat_id": external_id, "user_id": me["id"]})
        except PlatformError as e:
            return AccountCheck(False, detail=e.message, reauth=e.reauth)
        can_post = member.get("status") == "creator" or bool(member.get("can_post_messages"))
        if member.get("status") not in ("administrator", "creator") or not can_post:
            return AccountCheck(False, detail="Бот должен быть администратором канала с правом публикации сообщений", reauth=True)
        return AccountCheck(True, display_name=chat.get("title", ""), external_id=str(chat.get("id", external_id)), handle=f"@{chat['username']}" if chat.get("username") else "", scopes=["post_messages"])

    def fetch_metrics(self, external_id: str, token: str, *, fmt: str = "post") -> dict[str, Any]:
        """Bot API не отдаёт просмотры/пересылки канала. Возвращаем число подписчиков; остальное — ручной ввод/импорт."""
        chat_id = external_id.split("/")[0]
        try:
            cnt = self._call(token, "getChatMemberCount", data={"chat_id": chat_id})
        except PlatformError as e:
            return {"unavailable": ["views", "reach", "likes", "shares"], "error": e.message}
        return {"followers": int(cnt), "unavailable": ["views", "reach", "likes", "comments", "shares"], "note": "Telegram Bot API не предоставляет статистику постов канала"}


__all__ = ["TelegramAdapter", "AttemptOutcome"]
