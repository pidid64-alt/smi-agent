import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from smi_agent.core.enums import PubState
from smi_agent.core.errors import Conflict, Forbidden, ValidationFailed
from smi_agent.db.models import (
    AuditLog,
    Content,
    Notification,
    PlatformAccount,
    Proposal,
    Publication,
    PublicationEvent,
    PublishAttempt,
    SecretRecord,
)
from smi_agent.publishing.adapters.sandbox import SandboxAdapter
from smi_agent.publishing.service import STUCK_AFTER
from smi_agent.security.rbac import Actor

PLATFORMS = ("telegram", "instagram", "facebook")


@pytest.fixture
def ready(llm_ctx, corpus, pipeline, admin):
    """Материал с проходящими проверками + подключённые аккаунты-песочницы трёх платформ. Возвращает (ctx, project_id, content_pk)."""
    ctx = llm_ctx
    pipeline(corpus)
    with ctx.db.read() as s:
        pid = s.scalars(select(Proposal).where(Proposal.slot == 1)).first().id
    content_pk = ctx.content.create(corpus, pid, {}, admin)
    with ctx.db.session() as s:
        for p in PLATFORMS:
            ctx.accounts.connect(s, admin, corpus, platform=p, sandbox=True, mode="manual", display_name=f"{p} demo")
    return ctx, corpus, content_pk


def _pubs(ctx, content_pk=None):
    with ctx.db.read() as s:
        q = select(Publication).order_by(Publication.id)
        if content_pk:
            q = q.where(Publication.content_pk == content_pk)
        return [(p.id, p.platform, p.state) for p in s.scalars(q)]


def _create(ctx, project, content_pk, admin, **kw):
    with ctx.db.session() as s:
        return [p.id for p in ctx.publishing.create_for_content(s, project, content_pk, admin, **kw)]


def _approve_all(ctx, project, ids, admin, schedule=None):
    with ctx.db.session() as s:
        for i in ids:
            ctx.publishing.approve(s, project, i, admin, schedule=schedule)


def _set_failure(ctx, platform, mode):
    with ctx.db.session() as s:
        acc = s.scalars(select(PlatformAccount).where(PlatformAccount.platform == platform)).first()
        acc.settings = {**acc.settings, "sandbox_failure": mode}


def test_happy_path_states_and_audit(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin)
    assert {st for _i, _p, st in _pubs(ctx)} == {"awaiting_approval"}
    _approve_all(ctx, project, ids, admin)
    assert {st for _i, _p, st in _pubs(ctx)} == {"scheduled"}
    res = ctx.publishing.run_due()
    assert len(res) == 3 and all(r["state"] == "published" for r in res)
    with ctx.db.read() as s:
        for p in s.scalars(select(Publication)):
            assert p.external_id.startswith("sbx-") and p.published_at and p.attempts == 1 and p.approved_by == "user:1"
            states = [e.to_state for e in s.scalars(select(PublicationEvent).where(PublicationEvent.publication_id == p.id).order_by(PublicationEvent.id))]
            assert states == ["draft", "awaiting_approval", "scheduled", "publishing", "published"]
        actions = {a.action for a in s.scalars(select(AuditLog))}
        assert {"publication.create", "publication.approve", "publication.published", "account.connect"} <= actions
        assert s.get(Content, cpk).status == "published"
    assert ctx.publishing.run_due() == []  # повторный запуск ничего не делает


def test_create_is_idempotent(ready, admin):
    ctx, project, cpk = ready
    a = _create(ctx, project, cpk, admin)
    b = _create(ctx, project, cpk, admin)
    assert a == b and len(_pubs(ctx)) == 3
    with ctx.db.read() as s:
        keys = [p.idempotency_key for p in s.scalars(select(Publication))]
        assert len(set(keys)) == 3 and all(len(k) == 64 for k in keys)


def test_concurrent_workers_send_exactly_once(ready, admin, monkeypatch):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    sent = []
    orig = SandboxAdapter.publish

    def counting(self, c):
        sent.append(c.publication_id)
        return orig(self, c)

    monkeypatch.setattr(SandboxAdapter, "publish", counting)
    results = []

    def worker():
        results.append(ctx.publishing.publish_one(ids[0]))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(sent) == 1  # атомарный захват: одна отправка при шести конкурирующих воркерах
    assert sum(1 for r in results if r.get("claimed")) == 1 and sum(1 for r in results if r.get("state") == "published") == 1


def test_failed_checks_block_approval_until_fixed(ctx, corpus, pipeline, admin):
    pipeline(corpus)
    with ctx.db.read() as s:
        pid = s.scalars(select(Proposal).where(Proposal.slot == 1)).first().id
    cpk = ctx.content.create(corpus, pid, {}, admin)  # эвристика: проверка оригинальности не пройдена
    with ctx.db.session() as s:
        ctx.accounts.connect(s, admin, corpus, platform="telegram", sandbox=True)
    ids = _create(ctx, corpus, cpk, admin, platforms=["telegram"])
    with ctx.db.read() as s:
        assert s.get(Publication, ids[0]).state == "needs_review"
    with ctx.db.session() as s:
        with pytest.raises(ValidationFailed):
            ctx.publishing.approve(s, corpus, ids[0], admin)
    assert _pubs(ctx)[0][2] == "needs_review"
    # редактор переписывает текст своими словами → версия проходит проверки → публикация создаётся заново и подтверждается
    with ctx.db.session() as s:
        ctx.content.edit_version(s, corpus, cpk, "telegram", admin, body="<b>Ставка осталась 16,5%</b>\n\nРегулятор не стал менять ориентир для кредитов. Инфляция в августе — 12,3%.\n\nИсточники: NUR.KZ, Kursiv.media")
    states = _pubs(ctx)
    assert states[0][2] == "cancelled" and states[1][2] == "awaiting_approval"
    _approve_all(ctx, corpus, [states[1][0]], admin)
    assert ctx.publishing.run_due()[0]["state"] == "published"


def test_editing_after_approval_invalidates_the_approval(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["facebook"])
    _approve_all(ctx, project, ids, admin)
    with ctx.db.session() as s:
        ctx.content.edit_version(s, project, cpk, "facebook", admin, body="Нацбанк сохранил ставку 16,5% — новая формулировка.\n\nИсточники: NUR.KZ")
    st = {i: s for i, _p, s in _pubs(ctx)}
    assert st[ids[0]] == "cancelled"  # подтверждение относилось к старому тексту
    assert ctx.publishing.run_due() == []  # новая версия ждёт нового подтверждения


def test_approval_requires_permission_and_not_ai(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    with ctx.db.session() as s:
        with pytest.raises(Forbidden):
            ctx.publishing.approve(s, project, ids[0], Actor.ai())
        with pytest.raises(Forbidden):
            ctx.publishing.approve(s, project, ids[0], Actor.user(5, "auditor"))
        ctx.publishing.approve(s, project, ids[0], Actor.user(2, "user"))


def test_preflight_rechecks_content_at_send_time(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    with ctx.db.session() as s:  # в обход сервиса портят текст (сбой/злоумышленник): проверки при отправке это поймают
        from smi_agent.db.models import PlatformVersion

        v = s.scalars(select(PlatformVersion).where(PlatformVersion.platform == "telegram", PlatformVersion.is_current.is_(True))).first()
        v.body = v.body + " Ключ: sk-abcdefghijklmnopqrstuvwxyz123456"
    res = ctx.publishing.run_due()[0]
    assert res["sent"] is False and res["state"] == "needs_review" and "Проверки не пройдены" in res["note"]
    assert _pubs(ctx)[0][2] == "needs_review"


@pytest.mark.parametrize("mode,outcome,state", [("connect", "not_sent", "scheduled"), ("rate_limit", "rate_limited", "scheduled"), ("auth", "failed_permanent", "error"), ("bad_request", "failed_permanent", "error"), ("timeout", "unknown", "error")])
def test_failure_modes_route_to_safe_states(ready, admin, mode, outcome, state):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    _set_failure(ctx, "telegram", mode)
    res = ctx.publishing.run_due()[0]
    assert res["outcome"] == outcome and res["state"] == state
    with ctx.db.read() as s:
        pub = s.get(Publication, ids[0])
        att = s.scalar(select(PublishAttempt).where(PublishAttempt.publication_id == ids[0]))
        assert att.outcome == outcome and att.finished_at and pub.last_error["code"]
        if state == "scheduled":
            assert pub.next_attempt_at > ctx.clock.now() and pub.claim_token == ""
        if mode == "auth":
            assert s.scalars(select(PlatformAccount).where(PlatformAccount.platform == "telegram")).first().status == "needs_reauth"


def test_retry_with_backoff_then_success(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    _set_failure(ctx, "telegram", "connect")
    assert ctx.publishing.run_due()[0]["state"] == "scheduled"
    assert ctx.publishing.run_due() == []  # пока не наступило время повтора — не трогаем
    _set_failure(ctx, "telegram", "")
    ctx.clock.advance(seconds=31)
    assert ctx.publishing.run_due()[0]["state"] == "published"
    with ctx.db.read() as s:
        assert s.get(Publication, ids[0]).attempts == 2


def test_attempts_exhausted_goes_to_error(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    _set_failure(ctx, "telegram", "connect")
    for _ in range(6):
        ctx.publishing.run_due()
        ctx.clock.advance(hours=2)
    assert _pubs(ctx)[0][2] == "error"
    with ctx.db.read() as s:
        assert s.get(Publication, ids[0]).attempts == 5


def test_unknown_outcome_forbids_resend_until_reconciled(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    _set_failure(ctx, "telegram", "timeout")  # ответ не пришёл, публикации НЕТ, но система этого не знает
    assert ctx.publishing.run_due()[0]["state"] == "error"
    ctx.clock.advance(hours=3)
    assert ctx.publishing.run_due() == []  # автоповтора нет
    with ctx.db.session() as s:
        with pytest.raises(Conflict) as e:
            ctx.publishing.retry(s, project, ids[0], admin)
        assert e.value.code == "outcome_unknown"
    _set_failure(ctx, "telegram", "")
    rec = ctx.publishing.reconcile(project, ids[0], admin)
    assert rec["status"] == "not_found" and rec["state"] == "scheduled"  # доказано: не опубликовано → повтор безопасен
    assert ctx.publishing.run_due()[0]["state"] == "published"


def test_lost_response_after_send_is_found_by_reconcile_no_duplicate(ready, admin, monkeypatch):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["facebook"])
    _approve_all(ctx, project, ids, admin)
    _set_failure(ctx, "facebook", "timeout_after_send")  # пост ВЫШЕЛ, но ответ потерян
    sends = []
    orig = SandboxAdapter.publish
    monkeypatch.setattr(SandboxAdapter, "publish", lambda self, c: (sends.append(1), orig(self, c))[1])
    assert ctx.publishing.run_due()[0]["state"] == "error"
    assert ctx.publishing.reconcile(project, ids[0], admin)["status"] == "found"
    assert _pubs(ctx)[0][2] == "published" and len(sends) == 1
    with ctx.db.session() as s:
        with pytest.raises(Conflict):
            ctx.publishing.retry(s, project, ids[0], admin)  # уже опубликовано


def test_manual_decisions_for_inconclusive_outcome(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    _set_failure(ctx, "telegram", "timeout")
    ctx.publishing.run_due()
    with ctx.db.session() as s:  # человек проверил канал: поста нет → повтор по его явному решению, фиксируется в аудите
        ctx.publishing.retry(s, project, ids[0], admin, confirm_not_published=True)
        assert s.get(Publication, ids[0]).state == "scheduled"
        log = s.scalars(select(AuditLog).where(AuditLog.action == "publication.retry")).one()
        assert log.details["unknown_outcome"] is True and log.details["user_confirmed_not_published"] is True
    _set_failure(ctx, "telegram", "timeout")
    ctx.publishing.run_due()
    with ctx.db.session() as s:  # или пост на месте → подтвердить ссылку
        ctx.publishing.confirm_published(s, project, ids[0], admin, "https://t.me/chan/5")
        assert s.get(Publication, ids[0]).state == "published"


def test_stuck_publishing_after_crash_is_reconciled_not_resent(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    now = ctx.clock.now()
    with ctx.db.session() as s:  # имитация падения процесса после захвата
        pub = s.get(Publication, ids[0])
        pub.state, pub.claim_token, pub.claimed_at, pub.attempts = "publishing", "deadbeef", now - STUCK_AFTER - timedelta(minutes=1), 1
        s.add(PublishAttempt(publication_id=pub.id, attempt_no=1, started_at=now - timedelta(minutes=7), outcome="started"))
    out = ctx.publishing.recover_stuck()
    assert out and out[0]["status"] == "not_found"  # в песочнице публикации нет → повтор безопасен
    with ctx.db.read() as s:
        att = s.scalar(select(PublishAttempt).where(PublishAttempt.publication_id == ids[0]))
        assert att.outcome == "unknown" and att.reconcile["status"] == "not_found"
    assert ctx.publishing.run_due()[0]["state"] == "published"


def test_one_platform_failure_does_not_block_the_others(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin)
    _approve_all(ctx, project, ids, admin)
    _set_failure(ctx, "instagram", "bad_request")
    res = {r["publication_id"]: r for r in ctx.publishing.run_due()}
    states = {plat: st for _i, plat, st in _pubs(ctx)}
    assert states == {"telegram": "published", "instagram": "error", "facebook": "published"}
    assert len(res) == 3


def test_cancel_rules(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin)
    _approve_all(ctx, project, ids[:2], admin)
    with ctx.db.session() as s:
        ctx.publishing.cancel(s, project, ids[0], admin, "передумали")
        assert s.get(Publication, ids[0]).state == "cancelled"
        with pytest.raises(Conflict):
            ctx.publishing.transition(s, s.get(Publication, ids[0]), PubState.SCHEDULED, admin)  # из «Отменено» назад нельзя
    ctx.publishing.run_due()
    with ctx.db.session() as s:
        with pytest.raises(Conflict) as e:
            ctx.publishing.cancel(s, project, ids[1], admin)
        assert e.value.code == "already_published"


def test_state_machine_rejects_illegal_transitions(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    with ctx.db.session() as s:
        pub = s.get(Publication, ids[0])
        for bad in (PubState.PUBLISHED, PubState.PUBLISHING, PubState.ERROR):
            with pytest.raises(Conflict):
                ctx.publishing.transition(s, pub, bad, admin)
        ctx.publishing.transition(s, pub, PubState.NEEDS_REVIEW, admin)
        ctx.publishing.transition(s, pub, PubState.AWAITING_APPROVAL, admin)


def test_account_revocation_cancels_queue_and_destroys_token(ready, admin):
    ctx, project, cpk = ready
    with ctx.db.session() as s:
        acc = s.scalars(select(PlatformAccount).where(PlatformAccount.platform == "telegram")).first()
        rec = ctx.secrets.put(s, admin, project, "tg-real", "123456789:AAH-fakefakefakefakefakefakefakefake")
        acc.secret_id, acc.settings = rec.id, {}  # превращаем в «настоящий» аккаунт с токеном
        acc_id, sid = acc.id, rec.id
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    with ctx.db.session() as s:
        ctx.accounts.revoke(s, admin, project, acc_id, "утечка токена")
    with ctx.db.read() as s:
        assert s.get(Publication, ids[0]).state == "cancelled"
        assert s.get(SecretRecord, sid).ciphertext == b"" and s.get(PlatformAccount, acc_id).status == "revoked"
        assert s.scalars(select(Notification).where(Notification.kind == "account_revoked")).first()
        assert s.scalars(select(AuditLog).where(AuditLog.action == "account.revoke")).first()
    with ctx.db.session() as s:
        from smi_agent.core.errors import Conflict as C

        with pytest.raises(C):
            ctx.accounts.token_for(s, admin, s.get(PlatformAccount, acc_id))


def test_passwords_of_external_platforms_are_not_accepted(ctx, project, admin):
    with ctx.db.session() as s:
        with pytest.raises(ValidationFailed):
            ctx.accounts.connect(s, admin, project, platform="instagram", token="my password is 12345", external_id="1784")
        with pytest.raises(ValidationFailed):
            ctx.accounts.connect(s, admin, project, platform="whatsapp", token="x", external_id="1")  # WhatsApp исключён из первого этапа


def test_tokens_do_not_leak_into_logs_or_audit(ready, admin, caplog):
    import logging

    ctx, project, cpk = ready
    tok = "123456789:AAH-fakefakefakefakefakefakefakefake"
    with ctx.db.session() as s:
        acc = s.scalars(select(PlatformAccount).where(PlatformAccount.platform == "telegram")).first()
        rec = ctx.secrets.put(s, admin, project, "tg-real2", tok)
        acc.secret_id = rec.id
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    _approve_all(ctx, project, ids, admin)
    with caplog.at_level(logging.DEBUG):
        ctx.publishing.run_due()
    assert tok not in caplog.text
    with ctx.db.read() as s:
        assert all(tok not in str(a.details) for a in s.scalars(select(AuditLog)))
        assert s.scalars(select(AuditLog).where(AuditLog.action == "secret.access")).first()  # доступ к токену фиксируется в аудите
        assert all(tok not in (p.last_error or {}).get("message", "") for p in s.scalars(select(Publication)))


def test_scheduling_modes(ready, admin):
    ctx, project, cpk = ready
    ids = _create(ctx, project, cpk, admin, platforms=["telegram"])
    now = ctx.clock.now()
    with ctx.db.session() as s:
        with pytest.raises(ValidationFailed):
            ctx.publishing.approve(s, project, ids[0], admin, schedule={"mode": "at", "at": (now - timedelta(hours=1)).isoformat()})
        pub = ctx.publishing.approve(s, project, ids[0], admin, schedule={"mode": "optimal"})
        assert pub.scheduled_at > now and pub.schedule["mode"] == "optimal" and "по умолчанию" in pub.schedule["reason"]
        local_hour = pub.scheduled_at.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Almaty")).hour
        assert local_hour in (9, 13, 19, 21) and not (0 <= local_hour < 7)  # часы по умолчанию для Telegram, не тихие часы
    assert ctx.publishing.run_due() == []  # время ещё не пришло
    ctx.clock.set(pub.scheduled_at + timedelta(minutes=1))
    assert ctx.publishing.run_due()[0]["state"] == "published"
