"""Модуль публикации (ТЗ §24–35, §61.17): конечный автомат состояний, захват задания (CAS), идемпотентность, сверка, повторы.

Главные гарантии:
  * отдельный модуль; сбой одной платформы не блокирует остальные (каждая публикация обрабатывается независимо);
  * повторная отправка запрещена, пока предыдущая попытка не доказана как «не опубликовано» (сверка / явное подтверждение человеком);
  * любая непройденная проверка запрещает автопубликацию; аварийный выключатель действует и в момент захвата задания.
"""

from __future__ import annotations

import hashlib
import logging
import secrets as pysecrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..content.render import render_final
from ..core.enums import AccountStatus, AttemptOutcome, PublishMode, PubState
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..db.models import (
    Content,
    Event,
    MediaAsset,
    Notification,
    PlatformAccount,
    PlatformVersion,
    Project,
    Proposal,
    Publication,
    PublicationEvent,
    PublishAttempt,
)
from ..security.rbac import Actor, Perm, authorize
from ..settings_model import load_project_settings
from .adapters.base import MediaFile, PublishContext
from .errors import PlatformError, classify_exception

log = logging.getLogger(__name__)
S = PubState
TRANSITIONS: dict[PubState, set[PubState]] = {
    S.DRAFT: {S.AWAITING_APPROVAL, S.NEEDS_REVIEW, S.SCHEDULED, S.CANCELLED},
    S.AWAITING_APPROVAL: {S.SCHEDULED, S.NEEDS_REVIEW, S.CANCELLED, S.DRAFT},
    S.NEEDS_REVIEW: {S.AWAITING_APPROVAL, S.CANCELLED, S.DRAFT},
    S.SCHEDULED: {S.PUBLISHING, S.AWAITING_APPROVAL, S.NEEDS_REVIEW, S.CANCELLED},
    S.PUBLISHING: {S.PUBLISHED, S.ERROR, S.SCHEDULED, S.NEEDS_REVIEW, S.AWAITING_APPROVAL, S.CANCELLED},
    S.ERROR: {S.SCHEDULED, S.NEEDS_REVIEW, S.CANCELLED, S.PUBLISHED, S.AWAITING_APPROVAL},
    S.PUBLISHED: set(),
    S.CANCELLED: set(),
}
MAX_ATTEMPTS = 5
STUCK_AFTER = timedelta(minutes=5)


def idem_key(content_id: str, platform: str, account_id: int, version_id: int, text_hash: str, n: int = 0) -> str:
    return hashlib.sha256(f"{content_id}|{platform}|{account_id}|{version_id}|{text_hash}|{n}".encode()).hexdigest()


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * 2 ** max(0, attempts - 1)))


class PublishingService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ============================================================ автомат состояний
    def transition(self, s: Session, pub: Publication, to: PubState, actor: Actor, note: str = "") -> Publication:
        cur = PubState(pub.state)
        if to == cur:
            return pub
        if to not in TRANSITIONS[cur]:
            raise Conflict(f"Недопустимый переход: {cur.value} → {to.value}", code="bad_transition")
        pub.state, pub.updated_at = to.value, self.ctx.clock.now()
        s.add(PublicationEvent(publication_id=pub.id, ts=pub.updated_at, from_state=cur.value, to_state=to.value, actor=actor.label, note=note[:500]))
        s.flush()
        return pub

    def _notify(self, s: Session, pub: Publication, level: str, kind: str, title: str, body: str = "") -> None:
        s.add(Notification(project_id=pub.project_id, level=level, kind=kind, title=title[:300], body=body, payload={"publication_id": pub.id, "platform": pub.platform}, created_at=self.ctx.clock.now()))

    # ================================================================== создание
    def pick_account(self, s: Session, project_id: int, platform: str) -> PlatformAccount | None:
        accs = list(s.scalars(select(PlatformAccount).where(PlatformAccount.project_id == project_id, PlatformAccount.platform == platform, PlatformAccount.status == AccountStatus.CONNECTED.value).order_by(PlatformAccount.id)))
        return accs[0] if accs else None

    def create_for_content(self, s: Session, project_id: int, content_pk: int, actor: Actor, *, platforms: list[str] | None = None, schedule: dict[str, Any] | None = None, origin: str = "user") -> list[Publication]:
        content = self.ctx.content.get(s, project_id, content_pk)
        out: list[Publication] = []
        for v in self.ctx.content.current_versions(s, content.id):
            if platforms and v.platform not in platforms:
                continue
            acc = self.pick_account(s, project_id, v.platform)
            n_prev = s.scalar(select(func.count()).select_from(Publication).where(Publication.content_pk == content.id, Publication.platform == v.platform, Publication.platform_version_id == v.id)) or 0
            key = idem_key(content.content_id, v.platform, acc.id if acc else 0, v.id, v.text_hash, 0)
            existing = s.scalars(select(Publication).where(Publication.content_pk == content.id, Publication.platform == v.platform, Publication.platform_version_id == v.id, Publication.state != S.CANCELLED.value)).first()
            if existing is not None:
                out.append(existing)
                continue
            if n_prev:
                key = idem_key(content.content_id, v.platform, acc.id if acc else 0, v.id, v.text_hash, n_prev)
            now = self.ctx.clock.now()
            pub = Publication(project_id=project_id, content_pk=content.id, platform_version_id=v.id, platform=v.platform, account_id=acc.id if acc else None, version=v.version, state=S.DRAFT.value, idempotency_key=key, schedule=schedule or {}, origin=origin, created_at=now, updated_at=now)
            s.add(pub)
            s.flush()
            s.add(PublicationEvent(publication_id=pub.id, ts=now, from_state="", to_state=S.DRAFT.value, actor=actor.label, note="создано"))
            report = self.ctx.content.latest_report(s, v.id) or self.ctx.checks.run(s, content, v)
            if acc is None:
                pub.last_error = {"code": "no_account", "message": f"Не подключён аккаунт {v.platform}"}
            elif not report.passed:
                self.transition(s, pub, S.NEEDS_REVIEW, actor, "Проверки не пройдены: " + report.summary)
                self._notify(s, pub, "warning", "needs_review", f"Требует проверки: {content.content_id} → {v.platform}", report.summary)
            else:
                self.transition(s, pub, S.AWAITING_APPROVAL, actor, "Ожидает подтверждения")
                self._notify(s, pub, "info", "awaiting_approval", f"Ожидает подтверждения: {content.content_id} → {v.platform}")
            out.append(pub)
            self.ctx.audit.log(s, actor, "publication.create", project_id=project_id, target_type="publication", target_id=pub.id, details={"content_id": content.content_id, "platform": v.platform, "state": pub.state, "origin": origin})
        return out

    # ================================================================ подтверждение
    def approve(self, s: Session, project_id: int, pub_id: int, actor: Actor, *, schedule: dict[str, Any] | None = None) -> Publication:
        authorize(actor, Perm.PUBLISH_APPROVE)
        pub = self.get(s, project_id, pub_id)
        if pub.state not in (S.AWAITING_APPROVAL.value, S.NEEDS_REVIEW.value):
            raise Conflict(f"Подтвердить можно публикацию в состоянии «Ожидает подтверждения»; сейчас: {pub.state}", code="bad_state")
        acc = s.get(PlatformAccount, pub.account_id) if pub.account_id else None
        if acc is None or acc.status != AccountStatus.CONNECTED.value:
            raise ValidationFailed("Аккаунт платформы не подключён или требует повторной авторизации")
        v = s.get(PlatformVersion, pub.platform_version_id)
        if not v.is_current:
            raise Conflict("Текст был изменён — подтвердите актуальную версию", code="stale_version")
        content = s.get(Content, pub.content_pk)
        report = self.ctx.checks.run(s, content, v, reviewed=True)
        if not report.passed:
            if pub.state != S.NEEDS_REVIEW.value:
                self.transition(s, pub, S.NEEDS_REVIEW, actor, "Проверки не пройдены: " + report.summary)
            raise ValidationFailed("Публикация не подтверждена — проверки не пройдены: " + report.summary)
        if pub.state == S.NEEDS_REVIEW.value:
            self.transition(s, pub, S.AWAITING_APPROVAL, actor, "Проверки пройдены")
        now = self.ctx.clock.now()
        spec = schedule or pub.schedule or ({"mode": "optimal"} if acc.mode == PublishMode.SCHEDULED.value else {"mode": "now"})
        at, meta = self.ctx.scheduling.resolve(s, project_id, pub.platform, spec, now, account_id=acc.id)
        pub.scheduled_at, pub.schedule, pub.approved_by, pub.approved_at, pub.next_attempt_at = at, meta, actor.label, now, None
        self.transition(s, pub, S.SCHEDULED, actor, f"Подтверждено; время: {at.isoformat()} ({meta.get('reason', '')})")
        self.ctx.audit.log(s, actor, "publication.approve", project_id=project_id, target_type="publication", target_id=pub.id, details={"scheduled_at": at.isoformat(), "mode": meta.get("mode"), "platform": pub.platform})
        return pub

    def cancel(self, s: Session, project_id: int, pub_id: int, actor: Actor, reason: str = "") -> Publication:
        if actor.type == "user":
            authorize(actor, Perm.PUBLISH_CANCEL)
        pub = self.get(s, project_id, pub_id)
        if pub.state == S.PUBLISHING.value:
            pub.cancel_requested = True
            self.ctx.audit.log(s, actor, "publication.cancel_requested", project_id=project_id, target_type="publication", target_id=pub.id, details={"reason": reason})
            raise Conflict("Публикация уже отправляется — остановить нельзя; после завершения её можно удалить на платформе вручную", code="already_publishing")
        if pub.state == S.PUBLISHED.value:
            raise Conflict("Публикация уже опубликована: удалите пост на платформе вручную", code="already_published")
        self.transition(s, pub, S.CANCELLED, actor, reason or "отменено")
        self.ctx.audit.log(s, actor, "publication.cancel", project_id=project_id, target_type="publication", target_id=pub.id, details={"reason": reason})
        return pub

    def on_version_changed(self, s: Session, old: PlatformVersion, new: PlatformVersion, actor: Actor) -> None:
        """Правка текста отменяет подтверждения старой версии: новая версия проходит проверки и подтверждение заново."""
        pubs = list(s.scalars(select(Publication).where(Publication.platform_version_id == old.id, Publication.state.in_([S.DRAFT.value, S.AWAITING_APPROVAL.value, S.NEEDS_REVIEW.value, S.SCHEDULED.value, S.ERROR.value]))))
        for p in pubs:
            spec = dict(p.schedule or {})
            project_id = p.project_id
            self.transition(s, p, S.CANCELLED, actor, "Текст изменён — создана новая версия публикации")
            self.create_for_content(s, project_id, p.content_pk, actor, platforms=[p.platform], schedule=spec, origin=p.origin)

    # ====================================================================== чтение
    def get(self, s: Session, project_id: int, pub_id: int) -> Publication:
        pub = s.get(Publication, pub_id)
        if pub is None or pub.project_id != project_id:
            raise NotFound("Публикация не найдена")
        return pub

    def list(self, s: Session, project_id: int, *, states: list[str] | None = None, limit: int = 200) -> list[Publication]:
        q = select(Publication).where(Publication.project_id == project_id)
        if states:
            q = q.where(Publication.state.in_(states))
        return list(s.scalars(q.order_by(Publication.id.desc()).limit(limit)))

    def to_dict(self, s: Session, pub: Publication, *, detail: bool = False) -> dict[str, Any]:
        from ..core.enums import PUB_STATE_LABELS

        content = s.get(Content, pub.content_pk)
        acc = s.get(PlatformAccount, pub.account_id) if pub.account_id else None
        d = {
            "id": pub.id, "content_id": content.content_id, "content_pk": content.id, "title": content.title, "platform": pub.platform, "state": pub.state, "state_label": PUB_STATE_LABELS.get(PubState(pub.state), pub.state),
            "account": acc.display_name if acc else None, "account_id": pub.account_id, "account_mode": acc.mode if acc else None, "origin": pub.origin, "scheduled_at": pub.scheduled_at.isoformat() if pub.scheduled_at else None,
            "schedule": pub.schedule, "published_at": pub.published_at.isoformat() if pub.published_at else None, "external_id": pub.external_id, "external_url": pub.external_url, "attempts": pub.attempts,
            "last_error": pub.last_error, "needs_manual": pub.needs_manual, "approved_by": pub.approved_by, "version": pub.version, "created_at": pub.created_at.isoformat(),
        }  # fmt: skip
        if detail:
            d["events"] = [{"ts": e.ts.isoformat(), "from": e.from_state, "to": e.to_state, "actor": e.actor, "note": e.note} for e in s.scalars(select(PublicationEvent).where(PublicationEvent.publication_id == pub.id).order_by(PublicationEvent.id))]
            d["attempts_log"] = [
                {"no": a.attempt_no, "started_at": a.started_at.isoformat(), "outcome": a.outcome, "error_code": a.error_code, "message": a.message, "http_status": a.http_status, "latency_ms": a.latency_ms, "reconcile": a.reconcile}
                for a in s.scalars(select(PublishAttempt).where(PublishAttempt.publication_id == pub.id).order_by(PublishAttempt.attempt_no))
            ]  # fmt: skip
        return d

    # =============================================================== рабочий процесс
    def due_ids(self, limit: int = 20) -> list[int]:
        now = self.ctx.clock.now()
        with self.ctx.db.read() as s:
            q = select(Publication.id).where(Publication.state == S.SCHEDULED.value, Publication.scheduled_at <= now, Publication.cancel_requested.is_(False), or_(Publication.next_attempt_at.is_(None), Publication.next_attempt_at <= now)).order_by(Publication.scheduled_at).limit(limit)
            return list(s.scalars(q))

    def run_due(self, limit: int = 20) -> list[dict[str, Any]]:
        results = []
        for pid in self.due_ids(limit):
            try:
                results.append(self.publish_one(pid))
            except Exception as e:  # noqa: BLE001 — сбой одной публикации не должен останавливать остальные платформы
                log.exception("publish_one %s crashed", pid)
                results.append({"publication_id": pid, "error": type(e).__name__})
        return results

    def _save_progress(self, pub_id: int, claim: str, progress: dict[str, Any]) -> None:
        with self.ctx.db.session() as s:
            pub = s.get(Publication, pub_id)
            if pub is not None and pub.claim_token == claim:
                pub.progress = dict(progress)

    def _media_files(self, s: Session, version: PlatformVersion) -> list[MediaFile]:
        out: list[MediaFile] = []
        for m in version.media or []:
            a = s.get(MediaAsset, m["asset_id"])
            if a is None:
                continue
            path = Path(self.ctx.settings.media_dir) / a.storage_path
            out.append(MediaFile(a.id, m.get("role", ""), path, a.mime, f"{self.ctx.settings.media_base_url}/media/{a.public_token}.jpg", a.alt_text))
        return out

    def publish_one(self, pub_id: int) -> dict[str, Any]:
        ctx = self.ctx
        now = ctx.clock.now()
        claim = pysecrets.token_hex(16)
        system = Actor.system()
        # ---------------- фаза A: атомарный захват + предполётные проверки (короткая транзакция записи)
        with ctx.db.session() as s:
            res = s.execute(update(Publication).where(
                Publication.id == pub_id, Publication.state == S.SCHEDULED.value, Publication.cancel_requested.is_(False), Publication.scheduled_at <= now,
                or_(Publication.next_attempt_at.is_(None), Publication.next_attempt_at <= now),
            ).values(state=S.PUBLISHING.value, claim_token=claim, claimed_at=now, attempts=Publication.attempts + 1, updated_at=now))  # fmt: skip
            if res.rowcount != 1:
                return {"publication_id": pub_id, "claimed": False}
            pub = s.get(Publication, pub_id)
            s.refresh(pub)
            s.add(PublicationEvent(publication_id=pub.id, ts=now, from_state=S.SCHEDULED.value, to_state=S.PUBLISHING.value, actor="system", note=f"захвачено, попытка {pub.attempts}"))
            attempt = PublishAttempt(publication_id=pub.id, attempt_no=pub.attempts, started_at=now, outcome=AttemptOutcome.STARTED.value)
            s.add(attempt)
            s.flush()
            pre = self._preflight(s, pub, now)
            if pre is not None:
                attempt.outcome, attempt.finished_at, attempt.message = AttemptOutcome.NOT_SENT.value, now, pre["note"][:600]
                pub.attempts -= 1  # попытка отправки не начиналась
                pub.claim_token = ""
                self.transition(s, pub, pre["state"], system, pre["note"])
                pub.last_error = {"code": "preflight", "message": pre["note"]}
                if pre["state"] in (S.NEEDS_REVIEW, S.AWAITING_APPROVAL):
                    self._notify(s, pub, "warning", "preflight_block", f"Публикация не выполнена: {pre['note'][:120]}", pre["note"])
                return {"publication_id": pub_id, "claimed": True, "sent": False, "state": pre["state"].value, "note": pre["note"]}
            data = pre_data = self._snapshot(s, pub)
            attempt_id, attempt_no = attempt.id, attempt.attempt_no
        # ---------------- фаза B: обращение к платформе (вне транзакции БД)
        pctx = PublishContext(
            publication_id=pub_id, platform=data["platform"], fmt=data["fmt"], text=data["text"], parse_mode=data["parse_mode"], media=data["media"], account_external_id=data["external_id"], account_handle=data["handle"],
            token=data["token"], progress=dict(data["progress"]), idempotency_key=data["idem"], save_progress=lambda p: self._save_progress(pub_id, claim, p), settings=data["account_settings"], claimed_at=now,
        )  # fmt: skip
        t0 = ctx.clock.now()
        result, error = None, None
        try:
            result = data["adapter"].publish(pctx)
        except Exception as e:  # noqa: BLE001
            error = classify_exception(e)
            if not isinstance(e, PlatformError):
                log.exception("adapter crashed pub=%s", pub_id)
        finally:
            pctx.token = ""  # токен в памяти не задерживаем
            data["token"] = ""
        latency = int((ctx.clock.now() - t0).total_seconds() * 1000)
        return self._record_outcome(pub_id, claim, attempt_id, attempt_no, result, error, latency, pre_data["platform"])

    def _preflight(self, s: Session, pub: Publication, now: datetime) -> dict[str, Any] | None:
        """None — можно отправлять. Иначе {state, note}: публикация возвращается в безопасное состояние, отправка НЕ выполняется."""
        ctx = self.ctx
        if pub.approved_by is None:
            return {"state": S.AWAITING_APPROVAL, "note": "Нет подтверждения — отправка запрещена"}
        acc = s.get(PlatformAccount, pub.account_id) if pub.account_id else None
        if acc is None or acc.status != AccountStatus.CONNECTED.value:
            return {"state": S.NEEDS_REVIEW, "note": "Аккаунт платформы не подключён или отозван"}
        content = s.get(Content, pub.content_pk)
        autonomous = pub.origin == "autopilot" or pub.approved_by == "service:autopilot"
        if autonomous:
            ks = ctx.killswitch.blocking(s, pub.project_id, platform=pub.platform, account_id=acc.id, category=content.category)
            if ks is not None:
                return {"state": S.AWAITING_APPROVAL, "note": f"Автопилот остановлен ({ks.scope_type}{': ' + ks.scope_value if ks.scope_value else ''}) — требуется ручное подтверждение"}
        v = s.get(PlatformVersion, pub.platform_version_id)
        if not v.is_current:
            return {"state": S.CANCELLED, "note": "Версия текста устарела"}
        human = not autonomous
        report = ctx.checks.run(s, content, v, reviewed=human)
        if not report.passed:
            return {"state": S.NEEDS_REVIEW, "note": "Проверки не пройдены перед отправкой: " + report.summary}
        if autonomous and report.blocks_autopilot:
            return {"state": S.AWAITING_APPROVAL, "note": "Автопубликация запрещена проверками: " + report.summary}
        return None

    def _snapshot(self, s: Session, pub: Publication) -> dict[str, Any]:
        ctx = self.ctx
        acc = s.get(PlatformAccount, pub.account_id)
        v = s.get(PlatformVersion, pub.platform_version_id)
        cfg = load_project_settings(s.get(Project, pub.project_id).settings)
        human = not (pub.origin == "autopilot" or pub.approved_by == "service:autopilot")
        rendered = render_final(pub.platform, v, cfg, reviewed=human)
        token = ctx.accounts.token_for(s, Actor.ai("publisher", pub.project_id), acc, purpose=f"publish:{pub.id}")
        return {
            "platform": pub.platform, "fmt": v.format, "text": rendered.text, "parse_mode": rendered.parse_mode, "media": self._media_files(s, v), "external_id": acc.external_id, "handle": acc.handle,
            "token": token, "progress": dict(pub.progress or {}), "idem": pub.idempotency_key, "adapter": ctx.accounts.adapter_for(acc), "account_settings": dict(acc.settings or {}),
        }  # fmt: skip

    def _record_outcome(self, pub_id: int, claim: str, attempt_id: int, attempt_no: int, result: Any, error: PlatformError | None, latency: int, platform: str) -> dict[str, Any]:
        ctx = self.ctx
        system = Actor.system()
        with ctx.db.session() as s:
            pub = s.get(Publication, pub_id)
            att = s.get(PublishAttempt, attempt_id)
            now = ctx.clock.now()
            if pub is None or pub.claim_token != claim:
                return {"publication_id": pub_id, "claimed": True, "sent": True, "stale_claim": True}
            att.finished_at, att.latency_ms = now, latency
            if error is None:
                att.outcome, att.external_id = AttemptOutcome.SUCCESS.value, result.external_id
                pub.external_id, pub.external_url, pub.published_at, pub.last_error, pub.needs_manual = result.external_id, result.url, now, {}, False
                self.transition(s, pub, S.PUBLISHED, system, f"опубликовано: {result.external_id}")
                self._after_published(s, pub)
                self._notify(s, pub, "info", "published", f"Опубликовано: {pub.platform}", result.url or result.external_id)
                ctx.audit.log(s, system, "publication.published", project_id=pub.project_id, target_type="publication", target_id=pub.id, details={"platform": pub.platform, "external_id": result.external_id, "attempt": attempt_no})
                return {"publication_id": pub_id, "claimed": True, "sent": True, "state": "published", "external_id": result.external_id}
            att.outcome, att.error_code, att.http_status, att.message, att.response_excerpt = error.outcome.value, error.code, error.http_status, error.message, error.excerpt
            pub.last_error = {"code": error.code, "message": error.message, "outcome": error.outcome.value, "at": now.isoformat()}
            acc = s.get(PlatformAccount, pub.account_id) if pub.account_id else None
            if error.reauth and acc is not None:
                acc.status, acc.last_error = AccountStatus.NEEDS_REAUTH.value, error.message[:500]
                self._notify(s, pub, "critical", "reauth", f"Требуется повторная авторизация: {acc.display_name}", error.message)
            if error.outcome in (AttemptOutcome.NOT_SENT, AttemptOutcome.RATE_LIMITED):
                if pub.attempts >= MAX_ATTEMPTS:
                    self.transition(s, pub, S.ERROR, system, f"исчерпаны попытки ({MAX_ATTEMPTS}): {error.message}")
                    self._notify(s, pub, "warning", "publish_error", f"Ошибка публикации ({pub.platform})", error.message)
                else:
                    delay = backoff(pub.attempts)
                    if error.retry_after:
                        delay = max(delay, timedelta(seconds=error.retry_after))
                    pub.next_attempt_at = now + delay
                    pub.claim_token = ""
                    self.transition(s, pub, S.SCHEDULED, system, f"{error.outcome.value}: повтор через {int(delay.total_seconds())} с — {error.message}")
            elif error.outcome == AttemptOutcome.FAILED_PERMANENT:
                self.transition(s, pub, S.ERROR, system, f"отклонено платформой: {error.message}")
                self._notify(s, pub, "warning", "publish_error", f"Публикация отклонена ({pub.platform})", error.message)
            else:  # UNKNOWN — повторная отправка запрещена до сверки
                pub.needs_manual = False
                self.transition(s, pub, S.ERROR, system, f"исход неизвестен, повторная отправка запрещена до сверки: {error.message}")
                self._notify(s, pub, "warning", "publish_unknown", f"Исход публикации неизвестен ({pub.platform})", "Запущена сверка. Автоматический повтор отключён, пока не доказано, что пост не вышел.")
            ctx.audit.log(s, system, "publication.attempt_failed", project_id=pub.project_id, target_type="publication", target_id=pub.id, outcome="error", details={"platform": pub.platform, "outcome": error.outcome.value, "code": error.code, "attempt": attempt_no})
            return {"publication_id": pub_id, "claimed": True, "sent": error.outcome == AttemptOutcome.UNKNOWN, "state": pub.state, "outcome": error.outcome.value, "code": error.code}

    def _after_published(self, s: Session, pub: Publication) -> None:
        content = s.get(Content, pub.content_pk)
        if content.status == "draft":
            content.status = "published"
        if content.event_id:
            ev = s.get(Event, content.event_id)
            if ev is not None:
                ev.stage = "published"
        if content.proposal_id:
            p = s.get(Proposal, content.proposal_id)
            if p is not None and p.status == "selected":
                p.status = "executed"

    # =================================================================== сверка
    def _reconcile_ctx(self, s: Session, pub: Publication) -> tuple[Any, PublishContext]:
        acc = s.get(PlatformAccount, pub.account_id)
        v = s.get(PlatformVersion, pub.platform_version_id)
        cfg = load_project_settings(s.get(Project, pub.project_id).settings)
        human = not (pub.origin == "autopilot" or pub.approved_by == "service:autopilot")
        rendered = render_final(pub.platform, v, cfg, reviewed=human)
        token = self.ctx.accounts.token_for(s, Actor.ai("publisher", pub.project_id), acc, purpose=f"reconcile:{pub.id}")
        pctx = PublishContext(
            publication_id=pub.id, platform=pub.platform, fmt=v.format, text=rendered.text, parse_mode=rendered.parse_mode, media=[], account_external_id=acc.external_id, account_handle=acc.handle, token=token,
            progress=dict(pub.progress or {}), idempotency_key=pub.idempotency_key, settings=dict(acc.settings or {}), claimed_at=pub.claimed_at,
        )  # fmt: skip
        return self.ctx.accounts.adapter_for(acc), pctx

    def reconcile(self, project_id: int | None, pub_id: int, actor: Actor | None = None) -> dict[str, Any]:
        """Сверка с платформой: found → опубликовано; not_found → повтор безопасен; unknown → только ручное решение."""
        actor = actor or Actor.system()
        ctx = self.ctx
        with ctx.db.session() as s:
            pub = s.get(Publication, pub_id)
            if pub is None or (project_id is not None and pub.project_id != project_id):
                raise NotFound("Публикация не найдена")
            if pub.state not in (S.ERROR.value, S.PUBLISHING.value):
                raise Conflict("Сверка нужна только для публикаций в состоянии «Ошибка»/«Публикуется»", code="bad_state")
            adapter, pctx = self._reconcile_ctx(s, pub)
        try:
            res = adapter.reconcile(pctx)
        finally:
            pctx.token = ""
        with ctx.db.session() as s:
            pub = s.get(Publication, pub_id)
            now = ctx.clock.now()
            att = s.scalars(select(PublishAttempt).where(PublishAttempt.publication_id == pub.id).order_by(PublishAttempt.attempt_no.desc()).limit(1)).first()
            if att is not None:
                att.reconcile = {"status": res.status, "detail": res.detail, "at": now.isoformat()}
            if res.status == "found":
                pub.external_id, pub.external_url, pub.published_at, pub.last_error, pub.needs_manual = res.external_id, res.url, pub.published_at or now, {}, False
                if pub.state == S.PUBLISHING.value:
                    self.transition(s, pub, S.ERROR, actor, "сверка: публикация найдена")
                self.transition(s, pub, S.PUBLISHED, actor, f"сверка: публикация найдена ({res.detail})")
                self._after_published(s, pub)
            elif res.status == "not_found":
                pub.next_attempt_at, pub.claim_token, pub.needs_manual = now, "", False
                if pub.state == S.PUBLISHING.value:
                    self.transition(s, pub, S.ERROR, actor, "сверка: публикации нет")
                self.transition(s, pub, S.SCHEDULED, actor, f"сверка: публикации нет, повтор безопасен ({res.detail})")
            else:
                pub.needs_manual = True
                if pub.state == S.PUBLISHING.value:
                    self.transition(s, pub, S.ERROR, actor, "исход не удалось установить")
                self._notify(s, pub, "critical", "needs_manual", f"Нужна ручная проверка: {pub.platform}", f"{res.detail}. Проверьте аккаунт и подтвердите результат в карточке публикации.")
            ctx.audit.log(s, actor, "publication.reconcile", project_id=pub.project_id, target_type="publication", target_id=pub.id, details={"result": res.status, "detail": res.detail})
            return {"publication_id": pub_id, "status": res.status, "detail": res.detail, "state": pub.state}

    def recover_stuck(self) -> list[dict[str, Any]]:
        """Публикации, «зависшие» в состоянии «Публикуется» после сбоя процесса: считаем исход неизвестным и сверяем перед любым повтором."""
        now = self.ctx.clock.now()
        with self.ctx.db.session() as s:
            stuck = list(s.scalars(select(Publication).where(Publication.state == S.PUBLISHING.value, Publication.claimed_at < now - STUCK_AFTER)))
            ids = []
            for pub in stuck:
                att = s.scalars(select(PublishAttempt).where(PublishAttempt.publication_id == pub.id).order_by(PublishAttempt.attempt_no.desc()).limit(1)).first()
                if att is not None and att.outcome == AttemptOutcome.STARTED.value:
                    att.outcome, att.finished_at, att.message = AttemptOutcome.UNKNOWN.value, now, "Процесс прерван во время отправки; исход неизвестен"
                ids.append(pub.id)
        return [self.reconcile(None, i) for i in ids]

    # ============================================== решения человека при неизвестном исходе
    def retry(self, s: Session, project_id: int, pub_id: int, actor: Actor, *, confirm_not_published: bool = False) -> Publication:
        authorize(actor, Perm.PUBLISH_APPROVE)
        pub = self.get(s, project_id, pub_id)
        if pub.state != S.ERROR.value:
            raise Conflict("Повторить можно публикацию в состоянии «Ошибка»", code="bad_state")
        att = s.scalars(select(PublishAttempt).where(PublishAttempt.publication_id == pub.id).order_by(PublishAttempt.attempt_no.desc()).limit(1)).first()
        unknown = att is not None and att.outcome in (AttemptOutcome.UNKNOWN.value, AttemptOutcome.STARTED.value)
        if unknown and not confirm_not_published:
            raise Conflict("Исход прошлой попытки неизвестен: повтор может привести к дублю. Сначала выполните сверку либо подтвердите, что вы проверили аккаунт и поста там нет.", code="outcome_unknown")
        now = self.ctx.clock.now()
        pub.next_attempt_at, pub.claim_token, pub.needs_manual, pub.cancel_requested = now, "", False, False
        if pub.scheduled_at is None or pub.scheduled_at > now:
            pub.scheduled_at = now
        self.transition(s, pub, S.SCHEDULED, actor, "повтор по решению пользователя" + (" (подтверждено, что поста нет)" if unknown else ""))
        self.ctx.audit.log(s, actor, "publication.retry", project_id=project_id, target_type="publication", target_id=pub.id, details={"unknown_outcome": unknown, "user_confirmed_not_published": confirm_not_published})
        return pub

    def confirm_published(self, s: Session, project_id: int, pub_id: int, actor: Actor, external_url: str = "") -> Publication:
        authorize(actor, Perm.PUBLISH_APPROVE)
        pub = self.get(s, project_id, pub_id)
        if pub.state != S.ERROR.value:
            raise Conflict("Подтвердить вручную можно публикацию в состоянии «Ошибка»", code="bad_state")
        pub.external_url, pub.published_at, pub.needs_manual, pub.last_error = external_url[:500], self.ctx.clock.now(), False, {}
        self.transition(s, pub, S.PUBLISHED, actor, "пользователь подтвердил, что пост опубликован")
        self._after_published(s, pub)
        self.ctx.audit.log(s, actor, "publication.confirm_published", project_id=project_id, target_type="publication", target_id=pub.id, details={"url": external_url[:200]})
        return pub
