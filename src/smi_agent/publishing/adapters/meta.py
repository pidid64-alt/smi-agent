"""Meta Graph API: Instagram (контейнеры → опрос статуса → media_publish) и Facebook Pages (/feed, /photos).

Идемпотентность Instagram: прогресс (id дочерних контейнеров, контейнера, media_id) сохраняется после каждого шага;
повтор не создаёт дубликатов и не вызывает media_publish, если media_id уже получен.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from ...core.enums import AttemptOutcome
from ..errors import PlatformError, classify_http
from .base import AccountCheck, PlatformHttp, PublishContext, PublishResult, ReconcileResult

_RATE_CODES = {4, 17, 32, 341, 613, 80001, 80002, 80006}
_AUTH_CODES = {102, 190, 10, 200, 459, 463, 467}


def graph_error(r: Any) -> PlatformError:
    try:
        err = (r.json() or {}).get("error", {})
    except ValueError:
        err = {}
    code, sub = err.get("code"), err.get("error_subcode")
    msg = str(err.get("message", ""))[:300]
    excerpt = f"code={code} subcode={sub} {msg}"
    if code in _RATE_CODES or sub == 2207042 or (code == 9):
        return PlatformError(AttemptOutcome.RATE_LIMITED, f"graph_{code}_{sub}", "Лимит публикаций/запросов платформы исчерпан", http_status=r.status_code, retry_after=900, excerpt=excerpt)
    if code in _AUTH_CODES or r.status_code in (401, 403):
        return PlatformError(AttemptOutcome.FAILED_PERMANENT, f"graph_{code}", "Токен недействителен, истёк или не хватает прав", http_status=r.status_code, reauth=True, excerpt=excerpt)
    if code in (1, 2) or err.get("is_transient"):
        return PlatformError(AttemptOutcome.UNKNOWN, f"graph_{code}", "Временный сбой платформы: исход публикации неизвестен", http_status=r.status_code, excerpt=excerpt)
    return classify_http(r.status_code, body_excerpt=excerpt, platform_code=f"graph_{code}_{sub}")


class _Graph:
    def __init__(self, http: PlatformHttp, base: str, version: str, sleep: Callable[[float], None] = time.sleep):
        self.http, self.root, self.sleep = http, f"{base.rstrip('/')}/{version}", sleep

    def call(self, method: str, path: str, token: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.http.request(method, f"{self.root}/{path.lstrip('/')}", token=token, params=params, data=data, files=files)
        if r.status_code >= 400:
            raise graph_error(r)
        try:
            return r.json()
        except ValueError as e:
            raise PlatformError(AttemptOutcome.UNKNOWN, "bad_json", "Некорректный ответ платформы", http_status=r.status_code) from e


def _prepublish(fn: Callable[[], Any]) -> Any:
    """До media_publish ничего не опубликовано: «неизвестный» исход шага подготовки безопасно повторить."""
    try:
        return fn()
    except PlatformError as e:
        if e.outcome == AttemptOutcome.UNKNOWN:
            e.outcome = AttemptOutcome.NOT_SENT
        raise


class InstagramAdapter:
    platform = "instagram"

    def __init__(self, http: PlatformHttp, base: str, version: str, sleep: Callable[[float], None] = time.sleep, poll_tries: int = 12, poll_interval: float = 5.0):
        self.g = _Graph(http, base, version, sleep)
        self.poll_tries, self.poll_interval = poll_tries, poll_interval

    def _wait_ready(self, container: str, token: str, *, video: bool) -> None:
        tries = self.poll_tries * (6 if video else 1)
        last = ""
        for _ in range(tries):
            st = _prepublish(lambda: self.g.call("GET", container, token, params={"fields": "status_code,status"}))
            last = st.get("status_code", "")
            if last == "FINISHED":
                return
            if last in ("ERROR", "EXPIRED"):
                raise PlatformError(AttemptOutcome.FAILED_PERMANENT, f"container_{last.lower()}", f"Instagram не смог обработать медиа ({last}): {st.get('status', '')}"[:300])
            self.g.sleep(self.poll_interval)
        raise PlatformError(AttemptOutcome.NOT_SENT, "container_timeout", f"Контейнер не успел обработаться (статус {last or 'неизвестен'}); повтор безопасен")

    def publish(self, ctx: PublishContext) -> PublishResult:
        ig, tok, prog = ctx.account_external_id, ctx.token, ctx.progress
        save = ctx.save_progress or (lambda _p: None)
        if prog.get("media_id"):  # уже опубликовано на прошлой попытке — повторно не публикуем
            return PublishResult(prog["media_id"], prog.get("permalink", ""))
        q = _prepublish(lambda: self.g.call("GET", f"{ig}/content_publishing_limit", tok, params={"fields": "quota_usage,config"}))
        if q.get("data"):
            d = q["data"][0]
            total = (d.get("config") or {}).get("quota_total", 100)
            if d.get("quota_usage", 0) >= total:
                raise PlatformError(AttemptOutcome.RATE_LIMITED, "ig_quota", f"Достигнут лимит публикаций Instagram за 24 часа ({total})", retry_after=3600)
        if not prog.get("container"):
            if not ctx.media:
                raise PlatformError(AttemptOutcome.FAILED_PERMANENT, "no_media", "Для Instagram нужно медиа")
            if ctx.fmt == "carousel":
                children: list[str] = list(prog.get("children", []))
                for m in ctx.media[len(children) : 10]:
                    child = _prepublish(lambda m=m: self.g.call("POST", f"{ig}/media", tok, data={"image_url": m.public_url, "is_carousel_item": "true"}))
                    children.append(child["id"])
                    save({**prog, "children": children})
                for c in children:
                    self._wait_ready(c, tok, video=False)
                cont = _prepublish(lambda: self.g.call("POST", f"{ig}/media", tok, data={"media_type": "CAROUSEL", "children": ",".join(children), "caption": ctx.text}))
            elif ctx.fmt == "reels":
                video = next((m for m in ctx.media if m.mime.startswith("video/")), None)
                if video is None:
                    raise PlatformError(AttemptOutcome.FAILED_PERMANENT, "no_video", "Для Reels нужен видеофайл (автоматическая генерация видео не выполняется)")
                cont = _prepublish(lambda: self.g.call("POST", f"{ig}/media", tok, data={"media_type": "REELS", "video_url": video.public_url, "caption": ctx.text, "share_to_feed": "true"}))
            else:
                cont = _prepublish(lambda: self.g.call("POST", f"{ig}/media", tok, data={"image_url": ctx.media[0].public_url, "caption": ctx.text}))
            prog = {**prog, "container": cont["id"]}
            save(prog)
        self._wait_ready(prog["container"], tok, video=ctx.fmt == "reels")
        res = self.g.call("POST", f"{ig}/media_publish", tok, data={"creation_id": prog["container"]})  # единственный «публикующий» вызов
        media_id = str(res.get("id", ""))
        if not media_id:
            raise PlatformError(AttemptOutcome.UNKNOWN, "no_media_id", "media_publish не вернул идентификатор публикации")
        prog = {**prog, "media_id": media_id}
        save(prog)
        permalink = ""
        try:
            permalink = self.g.call("GET", media_id, tok, params={"fields": "permalink"}).get("permalink", "")
        except PlatformError:
            pass
        return PublishResult(media_id, permalink, {"container": prog["container"]})

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        prog, tok, ig = ctx.progress, ctx.token, ctx.account_external_id
        if prog.get("media_id"):
            return ReconcileResult("found", prog["media_id"], prog.get("permalink", ""), "media_id сохранён")
        cont = prog.get("container")
        if not cont:
            return ReconcileResult("not_found", detail="Контейнер не создавался — публикация не выполнялась")
        try:
            st = self.g.call("GET", cont, tok, params={"fields": "status_code"}).get("status_code", "")
            if st == "PUBLISHED":
                recent = self.g.call("GET", f"{ig}/media", tok, params={"fields": "id,caption,permalink,timestamp", "limit": 10}).get("data", [])
                key = re.sub(r"\s+", " ", ctx.text)[:60]
                for m in recent:
                    if re.sub(r"\s+", " ", m.get("caption", ""))[:60] == key:
                        return ReconcileResult("found", m["id"], m.get("permalink", ""), "найдено по подписи среди последних публикаций")
                return ReconcileResult("unknown", detail="Контейнер опубликован, но публикацию не удалось сопоставить — проверьте аккаунт вручную")
            if st in ("FINISHED",):
                return ReconcileResult("not_found", detail="Контейнер готов, но не опубликован — повтор безопасен")
            if st in ("ERROR", "EXPIRED"):
                return ReconcileResult("not_found", detail=f"Контейнер в статусе {st} — публикации нет")
            return ReconcileResult("unknown", detail=f"Контейнер ещё обрабатывается ({st})")
        except PlatformError as e:
            return ReconcileResult("unknown", detail=e.message)

    def check_account(self, external_id: str, token: str) -> AccountCheck:
        try:
            me = self.g.call("GET", external_id, token, params={"fields": "id,username,name"})
            self.g.call("GET", f"{external_id}/content_publishing_limit", token, params={"fields": "quota_usage,config"})
        except PlatformError as e:
            return AccountCheck(False, detail=e.message, reauth=e.reauth)
        return AccountCheck(True, display_name=me.get("name") or me.get("username", ""), external_id=str(me.get("id", external_id)), handle=f"@{me['username']}" if me.get("username") else "", scopes=["instagram_content_publish"])

    def fetch_metrics(self, external_id: str, token: str, *, fmt: str = "post") -> dict[str, Any]:
        """Каждая метрика запрашивается отдельно: отсутствующие/устаревшие метрики не ломают весь сбор (Meta периодически их меняет)."""
        wanted = ["reach", "views", "likes", "comments", "shares", "saved", "total_interactions"]
        out: dict[str, Any] = {}
        unavailable: list[str] = []
        for m in wanted:
            try:
                d = self.g.call("GET", f"{external_id}/insights", token, params={"metric": m}).get("data", [])
                out[m] = int(d[0]["values"][0]["value"]) if d else None
                if not d:
                    unavailable.append(m)
            except PlatformError:
                unavailable.append(m)
        out["saves"] = out.pop("saved", None)
        out["unavailable"] = unavailable
        return out


class FacebookAdapter:
    platform = "facebook"

    def __init__(self, http: PlatformHttp, base: str, version: str, clock: Any = None):
        self.g = _Graph(http, base, version)
        self.clock = clock

    def publish(self, ctx: PublishContext) -> PublishResult:
        page, tok = ctx.account_external_id, ctx.token
        if ctx.fmt == "photo" and ctx.media:
            m = ctx.media[0]
            res = self.g.call("POST", f"{page}/photos", tok, data={"caption": ctx.text, "published": "true"}, files={"source": (m.path.name, m.path.read_bytes(), m.mime)})
            pid = str(res.get("post_id") or res.get("id", ""))
        else:
            res = self.g.call("POST", f"{page}/feed", tok, data={"message": ctx.text})
            pid = str(res.get("id", ""))
        if not pid:
            raise PlatformError(AttemptOutcome.UNKNOWN, "no_post_id", "Facebook не вернул идентификатор публикации")
        return PublishResult(pid, f"https://www.facebook.com/{pid}")

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        try:
            posts = self.g.call("GET", f"{ctx.account_external_id}/posts", ctx.token, params={"fields": "id,message,created_time,permalink_url", "limit": 10}).get("data", [])
        except PlatformError as e:
            return ReconcileResult("unknown", detail=e.message)
        key = re.sub(r"\s+", " ", ctx.text)[:80]
        for p in posts:
            if re.sub(r"\s+", " ", p.get("message", ""))[:80] == key:
                return ReconcileResult("found", p["id"], p.get("permalink_url", ""), "найдено среди последних публикаций страницы")
        now = self.clock.now() if self.clock else None
        if now and ctx.claimed_at and (now - ctx.claimed_at).total_seconds() >= 600:
            return ReconcileResult("not_found", detail="В ленте страницы нет публикации с таким текстом спустя 10 минут")
        return ReconcileResult("unknown", detail="Публикация пока не найдена; повторите сверку позже")

    def check_account(self, external_id: str, token: str) -> AccountCheck:
        try:
            me = self.g.call("GET", external_id, token, params={"fields": "id,name,username"})
        except PlatformError as e:
            return AccountCheck(False, detail=e.message, reauth=e.reauth)
        return AccountCheck(True, display_name=me.get("name", ""), external_id=str(me.get("id", external_id)), handle=me.get("username", ""), scopes=["pages_manage_posts"])

    def fetch_metrics(self, external_id: str, token: str, *, fmt: str = "post") -> dict[str, Any]:
        out: dict[str, Any] = {}
        unavailable: list[str] = []
        for key, metric in (("views", "post_media_view"), ("reach", "post_total_media_view_unique")):
            try:
                d = self.g.call("GET", f"{external_id}/insights", token, params={"metric": metric}).get("data", [])
                out[key] = int(d[0]["values"][0]["value"]) if d else None
                if not d:
                    unavailable.append(key)
            except PlatformError:
                unavailable.append(key)
        try:
            d = self.g.call("GET", external_id, token, params={"fields": "reactions.summary(true),comments.summary(true),shares"})
            out["likes"] = int(d.get("reactions", {}).get("summary", {}).get("total_count", 0))
            out["comments"] = int(d.get("comments", {}).get("summary", {}).get("total_count", 0))
            out["shares"] = int(d.get("shares", {}).get("count", 0))
        except PlatformError:
            unavailable += ["likes", "comments", "shares"]
        out["unavailable"] = unavailable
        return out
