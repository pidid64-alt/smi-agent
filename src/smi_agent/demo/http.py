"""Демо-платформы: ответы официальных API без сети.

Демо показывает полный цикл (подключение аккаунтов → публикация → сверка → метрики),
поэтому вызовы платформ должны что-то возвращать. Настоящие запросы при этом не
уходят никуда: HTTP-вызовы аккаунтов обслуживает подменённый транспорт
(`httpx.MockTransport`) — тот же приём, что и в тестах. В production демо запрещено,
поэтому транспорт создаётся только при `SMI_DEMO_MODE=1` вне production.

Метрики платформ намеренно недоступны (пустые `insights`, отказ на запрос реакций):
система честно покажет «недоступно», а не выдумает числа. Сбои (обрыв соединения,
таймаут, отказ платформы) имитируются через `failures` — используется в тестах.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx

DEMO_CHAT_ID = "-1001234567890"
DEMO_CHAT_USERNAME = "demo_editorial"
DEMO_BOT_ID = 4242


def _form(request: httpx.Request) -> dict[str, str]:
    """Поля формы из тела запроса (urlencoded). multipart разбирать не нужно: демо-платформы отвечают, не скачивая файлы."""
    try:
        raw = request.content.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — тело может отсутствовать
        return {}
    if not raw or raw.startswith("--"):
        return {}
    return {k: v[0] for k, v in parse_qs(raw).items()}


def _chat_id(chat: str) -> int | str:
    return int(chat) if chat.lstrip("-").isdigit() else chat


class DemoPlatforms:
    """Локальные ответы Telegram Bot API и Meta Graph API + журнал созданных публикаций (для сверки)."""

    def __init__(self) -> None:
        self.failures: dict[str, str] = {}  # «telegram» или идентификатор аккаунта → режим сбоя
        self.posts: dict[str, list[dict[str, str]]] = {}  # аккаунт → созданные публикации (сверка ищет по тексту)
        self._message_no = 1000
        self._container_no = 17890000000000
        self._published_no = 18000000000000

    def transport(self) -> httpx.BaseTransport:
        return httpx.MockTransport(self.handle_request)

    # ------------------------------------------------------------------ диспетчер
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "api.telegram.org":
            return self._telegram(request)
        if host in ("graph.facebook.com", "graph.instagram.com"):
            return self._graph(request)
        return httpx.Response(599, json={"error": {"message": "демо: хост не эмулируется"}})

    def _fail(self, request: httpx.Request, key: str, *, record: dict[str, str] | None = None) -> httpx.Response | None:
        """Имитация сбоя: исключение транспорта (connect/timeout) или ответ платформы с ошибкой. None — сбоя нет.

        Для режима timeout_after_send публикация сначала фиксируется (как в жизни: пост вышел, ответ потерялся),
        затем запрос падает по таймауту — сверка должна найти публикацию, а не отправить дубликат.
        """
        mode = self.failures.get(key)
        if not mode:
            return None
        if record is not None and mode == "timeout_after_send":
            self._add_post(record["account"], {"id": record["id"], "message": record["text"], "caption": record["text"], "permalink": record["permalink"]})
            raise httpx.ReadTimeout("демо: ответ потерян после отправки", request=request)
        if mode == "connect":
            raise httpx.ConnectError("демо: соединение не установлено", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("демо: таймаут", request=request)
        if mode == "rate_limit":
            return self._error(request, 429)
        if mode == "auth":
            return self._error(request, 403)
        if mode == "bad_request":
            return self._error(request, 400)
        return None

    @staticmethod
    def _error(request: httpx.Request, code: int) -> httpx.Response:
        desc = {429: "Too Many Requests: retry later", 403: "Forbidden: bot was kicked from the channel chat", 400: "Bad Request: invalid parameter"}[code]
        if (request.url.host or "") == "api.telegram.org":
            body: dict = {"ok": False, "error_code": code, "description": desc}
            if code == 429:
                body["parameters"] = {"retry_after": 30}
        else:
            body = {"error": {"code": {429: 4, 403: 190, 400: 100}[code], "message": desc}}
        return httpx.Response(code, json=body)

    def _add_post(self, account: str, post: dict[str, str]) -> None:
        self.posts.setdefault(account, []).insert(0, post)

    # -------------------------------------------------------------------- Telegram
    def _telegram(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/bot", 1)[-1].split("/", 1)
        method = parts[1] if len(parts) == 2 else ""
        chat = _form(request).get("chat_id") or DEMO_CHAT_ID
        failed = self._fail(request, "telegram")
        if failed is not None:
            return failed
        if method == "getMe":
            result: dict | list = {"id": DEMO_BOT_ID, "is_bot": True, "username": "smi_demo_bot"}
        elif method == "getChat":
            result = {"id": _chat_id(chat), "title": "Демо-канал", "username": DEMO_CHAT_USERNAME}
        elif method == "getChatMember":
            result = {"status": "administrator", "can_post_messages": True}
        elif method == "getChatMemberCount":
            result = 4200
        elif method in ("sendMessage", "sendPhoto", "sendMediaGroup"):
            self._message_no += 1
            result = {"message_id": self._message_no, "chat": {"id": _chat_id(chat), "username": DEMO_CHAT_USERNAME}}
        else:
            return httpx.Response(404, json={"ok": False, "error_code": 404, "description": f"демо: метод {method or '?'} не эмулируется"})
        return httpx.Response(200, json={"ok": True, "result": result})

    # ------------------------------------------------------------------------ Meta
    def _graph(self, request: httpx.Request) -> httpx.Response:
        seg = [p for p in request.url.path.split("/", 2)[-1].split("/") if p]  # без версии API
        head, tail = (seg[0] if seg else ""), (seg[1] if len(seg) > 1 else "")
        fields = _form(request)
        params = dict(request.url.params)
        for key, mode in self.failures.items():
            if key != "telegram" and key in request.url.path and mode:
                record = None
                if mode == "timeout_after_send" and request.method == "POST" and tail in ("feed", "photos"):
                    post_id = f"{head}_9000" if tail == "feed" else f"{head}_9001"
                    record = {"account": head, "id": post_id, "text": fields.get("message") or fields.get("caption") or "", "permalink": f"https://www.facebook.com/{post_id}"}
                failed = self._fail(request, key, record=record)
                if failed is not None:
                    return failed
                break
        if request.method == "POST" and tail == "media":
            self._container_no += 1
            return httpx.Response(200, json={"id": str(self._container_no)})
        if request.method == "POST" and tail == "media_publish":
            self._published_no += 1
            media_id = str(self._published_no)
            self._add_post(head, {"id": media_id, "caption": fields.get("caption", ""), "permalink": f"https://www.instagram.com/p/DEMO{media_id}/"})
            return httpx.Response(200, json={"id": media_id})
        if request.method == "POST" and tail in ("feed", "photos"):
            post_id = f"{head}_9000" if tail == "feed" else f"{head}_9001"
            self._add_post(head, {"id": post_id, "message": fields.get("message") or fields.get("caption", ""), "permalink_url": f"https://www.facebook.com/{post_id}"})
            return httpx.Response(200, json={"id": post_id, "post_id": post_id} if tail == "photos" else {"id": post_id})
        if tail == "content_publishing_limit":
            return httpx.Response(200, json={"data": [{"quota_usage": 0, "config": {"quota_total": 100}}]})
        if tail == "insights":
            return httpx.Response(200, json={"data": []})  # метрики недоступны — честный NULL, а не выдуманные числа
        if tail == "media" and request.method == "GET":
            return httpx.Response(200, json={"data": list(self.posts.get(head, []))})
        if tail == "posts":
            return httpx.Response(200, json={"data": list(self.posts.get(head, []))})
        if not tail:  # объект аккаунта/контейнера/публикации: что именно — по запрошенным полям
            f = params.get("fields", "")
            if "reactions" in f:
                return httpx.Response(400, json={"error": {"code": 100, "message": "демо: метрики публикации недоступны"}})
            if "followers_count" in f or "fan_count" in f:
                return httpx.Response(200, json={"id": head, "followers_count": 4200, "fan_count": 1500})
            if "status_code" in f:
                return httpx.Response(200, json={"status_code": "FINISHED", "status": "готово"})
            if "permalink" in f:
                return httpx.Response(200, json={"permalink": f"https://www.instagram.com/p/DEMO{head}/"})
            if f == "id,username,name":  # Instagram: проверка аккаунта
                return httpx.Response(200, json={"id": head, "username": DEMO_CHAT_USERNAME, "name": "Демо-аккаунт Instagram"})
            if f == "id,name,username":  # Facebook: проверка страницы
                return httpx.Response(200, json={"id": head, "name": "Демо-страница Facebook", "username": "demo.page"})
            return httpx.Response(200, json={"id": head, "name": "Демо-аккаунт", "username": DEMO_CHAT_USERNAME})
        return httpx.Response(200, json={"data": []})


def demo_platform_http(settings, platforms: DemoPlatforms | None = None) -> object:
    """PlatformHttp с демо-транспортом: вызовы платформ обслуживаются локально, без сети."""
    from ..publishing.adapters.base import PlatformHttp

    return PlatformHttp(settings, transport=(platforms or DemoPlatforms()).transport())
