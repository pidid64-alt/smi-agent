"""Адаптеры платформ проверяются на httpx.MockTransport: реальные API из этой среды недоступны (см. docs/INTEGRATIONS.md)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from smi_agent.config import Settings
from smi_agent.core.enums import AttemptOutcome
from smi_agent.publishing.adapters.base import MediaFile, PlatformHttp, PublishContext
from smi_agent.publishing.adapters.meta import FacebookAdapter, InstagramAdapter
from smi_agent.publishing.adapters.sandbox import SandboxAdapter
from smi_agent.publishing.adapters.telegram import TelegramAdapter
from smi_agent.publishing.errors import PlatformError, classify_exception, classify_http


def _ctx(platform="telegram", fmt="post", text="Текст поста", media=None, progress=None, ext="-100123", claimed=None):
    saved = []
    c = PublishContext(publication_id=7, platform=platform, fmt=fmt, text=text, parse_mode="HTML" if platform == "telegram" else None, media=media or [], account_external_id=ext, account_handle="@ch", token="SECRET-TOKEN", progress=dict(progress or {}), idempotency_key="k", save_progress=lambda p: saved.append(dict(p)), claimed_at=claimed)
    c.saved = saved  # type: ignore[attr-defined]
    return c


def _http(handler, settings=None):
    return PlatformHttp(settings or Settings(env="test"), transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ классификация исхода
@pytest.mark.parametrize("status,outcome", [(429, AttemptOutcome.RATE_LIMITED), (400, AttemptOutcome.FAILED_PERMANENT), (401, AttemptOutcome.FAILED_PERMANENT), (403, AttemptOutcome.FAILED_PERMANENT), (500, AttemptOutcome.UNKNOWN), (502, AttemptOutcome.UNKNOWN), (408, AttemptOutcome.UNKNOWN)])
def test_http_status_classification(status, outcome):
    assert classify_http(status).outcome == outcome
    assert classify_http(401).reauth and not classify_http(400).reauth


def test_network_exception_classification_only_connect_errors_are_safe_to_retry():
    assert classify_exception(httpx.ConnectError("x")).outcome == AttemptOutcome.NOT_SENT
    assert classify_exception(httpx.ConnectTimeout("x")).outcome == AttemptOutcome.NOT_SENT
    for exc in (httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x"), httpx.WriteTimeout("x")):
        assert classify_exception(exc).outcome == AttemptOutcome.UNKNOWN  # запрос мог дойти — дубль возможен
    assert classify_exception(ValueError("boom")).outcome == AttemptOutcome.UNKNOWN


def test_platform_http_blocks_unofficial_hosts_and_keeps_token_out_of_url():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"] = str(req.url), req.headers.get("authorization")
        return httpx.Response(200, json={})

    h = PlatformHttp(Settings(env="production"))  # без подмены транспорта: только официальные хосты
    with pytest.raises(PlatformError, match="не входит"):
        h.request("GET", "https://evil.example.com/x")
    h2 = PlatformHttp(Settings(env="test"), transport=httpx.MockTransport(handler))
    h2.request("GET", "https://graph.facebook.com/v26.0/me", token="TOK", params={"fields": "id"})
    assert "TOK" not in seen["url"] and seen["auth"] == "Bearer TOK"


# ---------------------------------------------------------------------------- Telegram
def test_telegram_send_message_success_and_formatting():
    captured = {}

    def handler(req: httpx.Request):
        captured["path"], captured["body"] = req.url.path, req.content.decode()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 55, "chat": {"id": -100123, "username": "mychannel"}}})

    ad = TelegramAdapter(_http(handler))
    res = ad.publish(_ctx(text="<b>Заголовок</b>"))
    assert res.external_id == "-100123/55" and res.url == "https://t.me/mychannel/55"
    assert captured["path"].endswith("/sendMessage") and "parse_mode=HTML" in captured["body"]


def test_telegram_errors_are_classified():
    def make(code, desc, params=None):
        return lambda req: httpx.Response(code, json={"ok": False, "error_code": code, "description": desc, "parameters": params or {}})

    with pytest.raises(PlatformError) as e:
        TelegramAdapter(_http(make(429, "Too Many Requests", {"retry_after": 17}))).publish(_ctx())
    assert e.value.outcome == AttemptOutcome.RATE_LIMITED and e.value.retry_after == 17
    with pytest.raises(PlatformError) as e:
        TelegramAdapter(_http(make(400, "Bad Request: message is too long"))).publish(_ctx())
    assert e.value.outcome == AttemptOutcome.FAILED_PERMANENT
    with pytest.raises(PlatformError) as e:
        TelegramAdapter(_http(make(403, "Forbidden: bot was kicked from the channel chat"))).publish(_ctx())
    assert e.value.reauth
    with pytest.raises(PlatformError) as e:
        TelegramAdapter(_http(make(502, "Bad Gateway"))).publish(_ctx())
    assert e.value.outcome == AttemptOutcome.UNKNOWN


def test_telegram_timeout_is_unknown_and_reconcile_refuses_to_guess():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    ad = TelegramAdapter(_http(handler))
    with pytest.raises(PlatformError) as e:
        ad.publish(_ctx())
    assert e.value.outcome == AttemptOutcome.UNKNOWN
    assert ad.reconcile(_ctx()).status == "unknown"  # Bot API не позволяет доказать отсутствие сообщения


def test_telegram_token_never_appears_in_error_messages():
    tok = "123456789:AAH-fakefakefakefakefakefakefakefake"

    def handler(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}", request=req)

    ctx = _ctx()
    ctx.token = tok
    with pytest.raises(PlatformError) as e:
        TelegramAdapter(_http(handler)).publish(ctx)
    assert tok not in e.value.message and tok not in str(e.value)


def test_telegram_check_account_requires_admin_with_post_rights():
    def make(status, can_post):
        def handler(req):
            m = req.url.path.rsplit("/", 1)[-1]
            res = {"getMe": {"id": 42, "is_bot": True}, "getChat": {"id": -100123, "title": "Канал", "username": "mychannel"}, "getChatMember": {"status": status, "can_post_messages": can_post}}[m]
            return httpx.Response(200, json={"ok": True, "result": res})

        return handler

    ok = TelegramAdapter(_http(make("administrator", True))).check_account("-100123", "t")
    assert ok.ok and ok.handle == "@mychannel"
    bad = TelegramAdapter(_http(make("member", False))).check_account("-100123", "t")
    assert not bad.ok and bad.reauth


def test_telegram_metrics_admit_what_is_unavailable():
    def handler(req):
        return httpx.Response(200, json={"ok": True, "result": 1234})

    m = TelegramAdapter(_http(handler)).fetch_metrics("-100123/5", "t")
    assert m["followers"] == 1234 and "views" in m["unavailable"]


# ---------------------------------------------------------------------------- Instagram
class IgServer:
    def __init__(self, *, quota=0, statuses=("IN_PROGRESS", "FINISHED"), publish_error=None):
        self.calls, self.statuses, self.quota, self.publish_error, self.n_container = [], list(statuses), quota, publish_error, 0

    def __call__(self, req: httpx.Request):
        path = req.url.path.split("/v26.0/", 1)[1]
        body = dict(httpx.QueryParams(req.content.decode())) if req.content else {}
        self.calls.append((req.method, path, body))
        if path.endswith("content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": self.quota, "config": {"quota_total": 100}}]})
        if path.endswith("/media") and req.method == "POST":
            self.n_container += 1
            return httpx.Response(200, json={"id": f"cont{self.n_container}"})
        if path.endswith("/media_publish"):
            if self.publish_error:
                return self.publish_error
            return httpx.Response(200, json={"id": "17900001"})
        if path.startswith("cont"):
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={"status_code": st, "id": path})
        if path.startswith("17900001"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/XYZ/"})
        return httpx.Response(404, json={"error": {"message": "nope", "code": 100}})


def _ig(server, **kw):
    return InstagramAdapter(_http(server), "https://graph.facebook.com", "v26.0", sleep=lambda s: None, **kw)


def _media(n=1, mime="image/jpeg"):
    return [MediaFile(i, "cover", Path(f"/tmp/x{i}.jpg"), mime, f"https://media.example/media/tok{i}.jpg") for i in range(n)]


def test_instagram_photo_flow_container_poll_publish():
    srv = IgServer()
    ctx = _ctx("instagram", "photo", "Подпись", media=_media(1), ext="178414")
    res = _ig(srv).publish(ctx)
    assert res.external_id == "17900001" and res.url.endswith("/p/XYZ/")
    kinds = [c[1].rsplit("/", 1)[-1] for c in srv.calls]
    assert kinds.index("media") < kinds.index("media_publish") and kinds.count("media_publish") == 1
    create = next(c for c in srv.calls if c[1].endswith("/media"))
    assert create[2]["image_url"].startswith("https://media.example/media/") and create[2]["caption"] == "Подпись"
    assert ctx.saved[-1]["media_id"] == "17900001"  # прогресс сохранён


def test_instagram_carousel_creates_children_then_parent():
    srv = IgServer()
    ctx = _ctx("instagram", "carousel", "Подпись", media=_media(4), ext="178414")
    _ig(srv).publish(ctx)
    creates = [c for c in srv.calls if c[1].endswith("/media") and c[0] == "POST"]
    assert len(creates) == 5 and sum(1 for c in creates if c[2].get("is_carousel_item") == "true") == 4
    assert creates[-1][2]["media_type"] == "CAROUSEL" and creates[-1][2]["children"].count(",") == 3


def test_instagram_resume_does_not_duplicate_container_or_publish():
    srv = IgServer()
    ctx = _ctx("instagram", "photo", "Подпись", media=_media(1), progress={"container": "cont9"})
    _ig(srv).publish(ctx)
    assert srv.n_container == 0  # контейнер из прогресса переиспользован
    srv2 = IgServer()
    done = _ctx("instagram", "photo", "Подпись", media=_media(1), progress={"container": "cont9", "media_id": "555", "permalink": "https://www.instagram.com/p/OLD/"})
    res = _ig(srv2).publish(done)
    assert res.external_id == "555" and not srv2.calls  # уже опубликовано: ни одного обращения к платформе


def test_instagram_quota_exhausted_is_rate_limited_before_any_upload():
    srv = IgServer(quota=100)
    with pytest.raises(PlatformError) as e:
        _ig(srv).publish(_ctx("instagram", "photo", media=_media(1)))
    assert e.value.outcome == AttemptOutcome.RATE_LIMITED and srv.n_container == 0


def test_instagram_container_error_is_permanent_and_timeout_is_safe_retry():
    with pytest.raises(PlatformError) as e:
        _ig(IgServer(statuses=("ERROR",))).publish(_ctx("instagram", "photo", media=_media(1)))
    assert e.value.outcome == AttemptOutcome.FAILED_PERMANENT
    with pytest.raises(PlatformError) as e:
        _ig(IgServer(statuses=("IN_PROGRESS",)), poll_tries=2).publish(_ctx("instagram", "photo", media=_media(1)))
    assert e.value.outcome == AttemptOutcome.NOT_SENT  # до media_publish ничего не опубликовано


def test_instagram_failure_at_publish_step_is_unknown_but_before_it_is_not_sent():
    err500 = httpx.Response(500, json={"error": {"message": "temporarily unavailable", "code": 2, "is_transient": True}})
    with pytest.raises(PlatformError) as e:
        _ig(IgServer(publish_error=err500)).publish(_ctx("instagram", "photo", media=_media(1)))
    assert e.value.outcome == AttemptOutcome.UNKNOWN  # именно на media_publish исход неизвестен

    def boom_on_create(req):
        if req.url.path.endswith("content_publishing_limit"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(500, json={"error": {"message": "x", "code": 2, "is_transient": True}})

    with pytest.raises(PlatformError) as e:
        _ig(boom_on_create).publish(_ctx("instagram", "photo", media=_media(1)))
    assert e.value.outcome == AttemptOutcome.NOT_SENT  # сбой при создании контейнера: публикации быть не могло


def test_graph_error_mapping_auth_and_rate_codes():
    bad_token = httpx.Response(400, json={"error": {"message": "Error validating access token", "code": 190}})
    with pytest.raises(PlatformError) as e:
        _ig(IgServer(publish_error=bad_token)).publish(_ctx("instagram", "photo", media=_media(1)))
    assert e.value.reauth and e.value.outcome == AttemptOutcome.FAILED_PERMANENT
    limit = httpx.Response(400, json={"error": {"message": "limit", "code": 9, "error_subcode": 2207042}})
    with pytest.raises(PlatformError) as e:
        _ig(IgServer(publish_error=limit)).publish(_ctx("instagram", "photo", media=_media(1)))
    assert e.value.outcome == AttemptOutcome.RATE_LIMITED


def test_instagram_reels_needs_video_file():
    with pytest.raises(PlatformError) as e:
        _ig(IgServer()).publish(_ctx("instagram", "reels", media=_media(1)))
    assert e.value.code == "no_video"


def test_instagram_reconcile_paths():
    def srv(status, recent=None):
        def h(req):
            p = req.url.path
            if p.endswith("/media") and req.method == "GET":
                return httpx.Response(200, json={"data": recent or []})
            return httpx.Response(200, json={"status_code": status})

        return h

    assert _ig(srv("X")).reconcile(_ctx("instagram", progress={})).status == "not_found"  # контейнер не создавался
    assert _ig(srv("X")).reconcile(_ctx("instagram", progress={"media_id": "9"})).status == "found"
    assert _ig(srv("FINISHED")).reconcile(_ctx("instagram", progress={"container": "c"})).status == "not_found"
    assert _ig(srv("IN_PROGRESS")).reconcile(_ctx("instagram", progress={"container": "c"})).status == "unknown"
    found = _ig(srv("PUBLISHED", [{"id": "77", "caption": "Подпись поста", "permalink": "https://www.instagram.com/p/A/"}])).reconcile(_ctx("instagram", text="Подпись поста", progress={"container": "c"}))
    assert found.status == "found" and found.external_id == "77"
    assert _ig(srv("PUBLISHED", [])).reconcile(_ctx("instagram", progress={"container": "c"})).status == "unknown"


def test_instagram_metrics_tolerate_missing_metrics():
    def h(req):
        metric = req.url.params.get("metric")
        if metric in ("reach", "likes"):
            return httpx.Response(200, json={"data": [{"values": [{"value": 120}]}]})
        return httpx.Response(400, json={"error": {"message": "metric deprecated", "code": 100}})

    m = _ig(h).fetch_metrics("179000", "t")
    assert m["reach"] == 120 and m["likes"] == 120 and "views" in m["unavailable"] and "saves" in m


# ---------------------------------------------------------------------------- Facebook
def test_facebook_feed_publish_and_reconcile_window():
    def h(req):
        if req.method == "POST":
            return httpx.Response(200, json={"id": "1234_5678"})
        return httpx.Response(200, json={"data": [{"id": "1234_1", "message": "Другой пост"}]})

    class Clock:
        def __init__(self, now):
            self._n = now

        def now(self):
            return self._n

    t0 = datetime(2026, 9, 30, 12, tzinfo=UTC)
    ad = FacebookAdapter(_http(h), "https://graph.facebook.com", "v26.0", Clock(t0 + timedelta(minutes=3)))
    res = ad.publish(_ctx("facebook", text="Текст", ext="1234"))
    assert res.external_id == "1234_5678"
    assert ad.reconcile(_ctx("facebook", text="Текст", ext="1234", claimed=t0)).status == "unknown"  # рано говорить «нет»
    late = FacebookAdapter(_http(h), "https://graph.facebook.com", "v26.0", Clock(t0 + timedelta(minutes=11)))
    assert late.reconcile(_ctx("facebook", text="Текст", ext="1234", claimed=t0)).status == "not_found"

    def with_post(req):
        return httpx.Response(200, json={"data": [{"id": "1234_9", "message": "Текст", "permalink_url": "https://fb.com/x"}]})

    assert FacebookAdapter(_http(with_post), "https://graph.facebook.com", "v26.0", Clock(t0)).reconcile(_ctx("facebook", text="Текст", ext="1234", claimed=t0)).status == "found"


# ---------------------------------------------------------------------------- песочница
def test_sandbox_modes():
    ad = SandboxAdapter("telegram")
    ok = ad.publish(_ctx(progress={}))
    assert ok.external_id.startswith("sbx-")
    for mode, outcome in (("connect", AttemptOutcome.NOT_SENT), ("rate_limit", AttemptOutcome.RATE_LIMITED), ("auth", AttemptOutcome.FAILED_PERMANENT), ("bad_request", AttemptOutcome.FAILED_PERMANENT), ("timeout", AttemptOutcome.UNKNOWN)):
        c = _ctx()
        c.settings = {"sandbox_failure": mode}
        with pytest.raises(PlatformError) as e:
            ad.publish(c)
        assert e.value.outcome == outcome
    c = _ctx()
    c.settings = {"sandbox_failure": "timeout_after_send"}
    with pytest.raises(PlatformError):
        ad.publish(c)
    assert c.saved[-1]["sandbox_done"] and ad.reconcile(_ctx(progress=c.saved[-1])).status == "found"
