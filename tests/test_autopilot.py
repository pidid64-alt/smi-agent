from datetime import timedelta

import pytest
from sqlalchemy import select

from smi_agent.core.errors import Forbidden, ValidationFailed
from smi_agent.db.models import (
    AuditLog,
    Event,
    KillSwitch,
    LearningEvent,
    Notification,
    PlatformAccount,
    Project,
    Proposal,
    Publication,
    UserAction,
)
from smi_agent.security.rbac import Actor
from tests.conftest import add_article


def _set_project(ctx, project, patch):
    with ctx.db.session() as s:
        p = s.get(Project, project)
        p.settings = {**(p.settings or {}), **patch}


def _accounts(ctx, project, admin, platforms=("telegram", "facebook"), mode="auto"):
    with ctx.db.session() as s:
        for plat in platforms:
            ctx.accounts.connect(s, admin, project, platform=plat, sandbox=True, mode=mode, display_name=f"{plat} demo")


def _policy(ctx, project, admin, enabled=True, **kw):
    with ctx.db.session() as s:
        ctx.autopilot.set_policy(s, admin, project, enabled=enabled, **kw)


@pytest.fixture
def auto(llm_ctx, corpus, pipeline, admin, clock):
    """Проект в режиме автопилота с включённой политикой; время — рабочий день, вне тихих часов (15:00 Алматы)."""
    ctx = llm_ctx
    ctx.settings.llm_daily_call_budget = 10_000
    pipeline(corpus)
    _accounts(ctx, corpus, admin)
    _policy(ctx, corpus, admin)
    _set_project(ctx, corpus, {"mode": "autopilot", "autopilot": {"min_trend_score": 40.0}})
    return ctx, corpus


def test_autopilot_is_off_by_default(ctx, corpus, pipeline):
    pipeline(corpus)
    assert ctx.autopilot.tick(corpus)["status"] == "off"
    with ctx.db.read() as s:
        assert s.query(Publication).count() == 0


def test_enabling_autopilot_requires_admin_and_is_audited(ctx, corpus, admin):
    with ctx.db.session() as s:
        with pytest.raises(Forbidden):
            ctx.autopilot.set_mode(s, Actor.user(2, "user"), corpus, "autopilot")
        with pytest.raises(Forbidden):
            ctx.autopilot.set_policy(s, Actor.user(2, "user"), corpus, enabled=True)
        with pytest.raises(ValidationFailed):
            ctx.autopilot.set_mode(s, admin, corpus, "yolo")
        assert ctx.autopilot.set_mode(s, admin, corpus, "autopilot")["mode"] == "autopilot"
        assert s.scalars(select(AuditLog).where(AuditLog.action == "agent.mode")).one().details == {"from": "learning", "to": "autopilot"}
        with pytest.raises(ValidationFailed):  # для политики и происшествий автопубликация запрещена принципиально
            ctx.autopilot.set_policy(s, admin, corpus, enabled=True, category="politics")


def test_autopilot_schedules_only_strong_verified_llm_content(auto):
    ctx, project = auto
    rec = ctx.autopilot.tick(project)
    assert rec["status"] == "scheduled" and rec["actions"]
    with ctx.db.read() as s:
        pubs = list(s.scalars(select(Publication)))
        assert {p.platform for p in pubs} == {"telegram", "facebook"}  # только аккаунты в режиме auto с включённой политикой
        assert all(p.origin == "autopilot" and p.state == "scheduled" and p.approved_by == "service:autopilot" for p in pubs)
        ev = s.get(Event, s.scalars(select(Proposal).where(Proposal.autopilot.is_(True))).first().event_id)
        assert ev.verification_status == "multi_confirmed" and ev.trend_score >= 40
        # выбор автопилота — не сигнал предпочтений пользователя (иначе петля самоподкрепления)
        assert s.query(UserAction).count() == 0 and s.query(LearningEvent).count() == 0
    # в момент публикации проверки выполняются заново и проходят
    ctx.clock.advance(days=1)
    assert all(r["state"] == "published" for r in ctx.publishing.run_due())


def test_autopilot_refuses_heuristic_text_without_llm(ctx, corpus, pipeline, admin):
    pipeline(corpus)
    _accounts(ctx, corpus, admin)
    _policy(ctx, corpus, admin)
    _set_project(ctx, corpus, {"mode": "autopilot"})
    rec = ctx.autopilot.tick(corpus)
    assert rec["status"] == "blocked" and any("LLM не настроена" in str(x) for x in rec["skipped"])
    with ctx.db.read() as s:
        assert s.query(Publication).count() == 0


def test_politics_and_sensitive_topics_are_never_auto_published(llm_ctx, project, pipeline, admin):
    ctx = llm_ctx
    add_article(ctx, project, "nur_kz", "ЦИК зарегистрировала 120 кандидатов на выборах в мажилис", "Избирательная комиссия зарегистрировала 120 кандидатов на выборах в мажилис. Выборы пройдут 15 ноября в 17 регионах страны. Кандидаты будут вести агитацию три недели.", minutes=0)
    add_article(ctx, project, "reuters", "Kazakhstan registers 120 candidates for parliament vote", "Kazakhstan's election commission registered 120 candidates for the parliament vote on 15 November in 17 regions, the commission said on Tuesday. Candidates will campaign for three weeks.", minutes=20)
    add_article(ctx, project, "bbc_world", "Kazakhstan sets parliament vote for 15 November", "Kazakhstan will hold a parliament vote on 15 November in 17 regions after the commission registered 120 candidates, officials said on Tuesday. The campaign lasts three weeks.", minutes=40)
    pipeline(project)
    _accounts(ctx, project, admin)
    _policy(ctx, project, admin)
    _set_project(ctx, project, {"mode": "autopilot", "autopilot": {"min_trend_score": 10.0}})
    rec = ctx.autopilot.tick(project)
    reasons = " ".join(str(x) for x in rec["skipped"])
    assert rec["status"] in ("nothing_suitable", "idle") and ("политическая" in reasons or "чувствительная" in reasons)
    with ctx.db.read() as s:
        assert s.query(Publication).count() == 0


def test_score_threshold_daily_cap_gap_and_quiet_hours(auto, admin):
    ctx, project = auto
    _set_project(ctx, project, {"mode": "autopilot", "autopilot": {"min_trend_score": 99.0}})
    rec = ctx.autopilot.tick(project)
    assert rec["status"] == "nothing_suitable" and any("ниже порога автопилота" in str(x) for x in rec["skipped"])
    # тихие часы (03:00 по Алматы = 22:00 UTC)
    _set_project(ctx, project, {"mode": "autopilot", "autopilot": {"min_trend_score": 40.0}})
    from datetime import UTC, datetime

    ctx.clock.set(datetime(2026, 9, 30, 22, 0, tzinfo=UTC))
    assert ctx.autopilot.tick(project)["status"] == "quiet"
    ctx.clock.set(datetime(2026, 9, 30, 10, 0, tzinfo=UTC))
    assert ctx.autopilot.tick(project)["status"] == "scheduled"
    with ctx.db.read() as s:
        first_at = min(p.scheduled_at for p in s.scalars(select(Publication)))
    # незадолго до запланированной публикации новый пост автопилота не ставится: минимальный интервал между постами
    ctx.clock.set(first_at - timedelta(minutes=10))
    assert ctx.autopilot.tick(project)["status"] == "gap"
    _set_project(ctx, project, {"mode": "autopilot", "autopilot": {"min_trend_score": 40.0, "max_posts_per_day": 2, "min_gap_minutes": 1}})
    assert ctx.autopilot.tick(project)["status"] == "daily_cap"  # 2 платформы = 2 публикации за сегодня уже есть


def test_accounts_in_manual_mode_or_without_policy_are_not_used(llm_ctx, corpus, pipeline, admin):
    ctx = llm_ctx
    pipeline(corpus)
    _accounts(ctx, corpus, admin, platforms=("telegram",), mode="manual")
    _set_project(ctx, corpus, {"mode": "autopilot"})
    assert ctx.autopilot.tick(corpus)["status"] == "no_accounts"
    _accounts(ctx, corpus, admin, platforms=("facebook",), mode="auto")
    rec = ctx.autopilot.tick(corpus)
    assert rec["status"] == "no_accounts" and any("политика автопилота не включена" in str(x) for x in rec["skipped"])


def test_co_editor_prepares_drafts_but_never_publishes(llm_ctx, corpus, pipeline, admin):
    ctx = llm_ctx
    pipeline(corpus)
    _accounts(ctx, corpus, admin, mode="manual")
    _set_project(ctx, corpus, {"mode": "co_editor"})
    rec = ctx.autopilot.tick(corpus)
    assert rec["status"] == "drafted" and rec["actions"][0]["type"] == "draft_prepared"
    with ctx.db.read() as s:
        by_platform = {p.platform: p for p in s.scalars(select(Publication))}
        assert by_platform["telegram"].state == by_platform["facebook"].state == "awaiting_approval"
        assert by_platform["instagram"].state == "draft" and by_platform["instagram"].last_error["code"] == "no_account"  # аккаунт Instagram не подключён
        assert all(p.origin == "co_editor" for p in by_platform.values())
    assert ctx.autopilot.tick(corpus)["status"] == "idle"  # повторно черновик для того же подбора не создаётся
    assert ctx.publishing.run_due() == []  # без подтверждения человека ничего не уходит


# ------------------------------------------------------------------------ аварийный выключатель
def test_kill_switch_blocks_autopilot_and_holds_scheduled_posts(auto, admin):
    ctx, project = auto
    assert ctx.autopilot.tick(project)["status"] == "scheduled"
    with ctx.db.session() as s:
        ks = ctx.killswitch.engage(s, Actor.user(2, "user"), project_id=project, scope_type="project", reason="ложное срабатывание")
        ks_id = ks.id
    with ctx.db.read() as s:
        assert {p.state for p in s.scalars(select(Publication))} == {"awaiting_approval"}  # приостановлены, не выйдут «задним числом»
        assert s.scalars(select(Notification).where(Notification.kind == "killswitch", Notification.level == "critical")).first()
    assert ctx.autopilot.tick(project)["status"] == "killed"
    ctx.clock.advance(days=1)
    assert ctx.publishing.run_due() == []
    # снять может только человек с правом kill.release
    with ctx.db.session() as s:
        for actor in (Actor.ai(), Actor.user(3, "user"), Actor.user(4, "auditor")):
            with pytest.raises(Forbidden):
                ctx.killswitch.release(s, actor, ks_id)
        ctx.killswitch.release(s, admin, ks_id, "разобрались")
        assert s.get(KillSwitch, ks_id).released_by == "user:1"
    with ctx.db.read() as s:
        assert {p.state for p in s.scalars(select(Publication))} == {"awaiting_approval"}  # после снятия — только ручное подтверждение
    assert ctx.autopilot.tick(project)["status"] in ("scheduled", "gap", "daily_cap", "nothing_suitable", "idle")


def test_kill_switch_scopes(llm_ctx, corpus, admin):
    ctx = llm_ctx
    _accounts(ctx, corpus, admin)
    with ctx.db.session() as s:
        acc = s.scalars(select(PlatformAccount).where(PlatformAccount.platform == "telegram")).first()
        ctx.killswitch.engage(s, admin, project_id=corpus, scope_type="platform", scope_value="facebook", reason="t")
        ctx.killswitch.engage(s, admin, project_id=corpus, scope_type="account", scope_value=str(acc.id), reason="t")
        ctx.killswitch.engage(s, admin, project_id=corpus, scope_type="category", scope_value="politics", reason="t")
        same = ctx.killswitch.engage(s, admin, project_id=corpus, scope_type="category", scope_value="politics", reason="повтор")
        assert len(ctx.killswitch.active(s, corpus)) == 3 and same.id  # повторное включение идемпотентно
        b = lambda **kw: ctx.killswitch.blocking(s, corpus, **kw)  # noqa: E731
        assert b(platform="facebook") and not b(platform="instagram") and b(account_id=acc.id) and not b(account_id=acc.id + 99)
        assert b(category="politics") and not b(category="economy") and not b()
        ctx.killswitch.engage(s, admin, project_id=None, scope_type="system", reason="авария")
        assert b() is not None and b(platform="instagram") is not None  # системный уровень блокирует всё
        with pytest.raises(ValidationFailed):
            ctx.killswitch.engage(s, admin, project_id=corpus, scope_type="planet", reason="x")
        with pytest.raises(ValidationFailed):
            ctx.killswitch.engage(s, admin, project_id=corpus, scope_type="platform", reason="нет значения")
        with pytest.raises(ValidationFailed):
            ctx.killswitch.engage(s, Actor.user(9, "user"), project_id=None, scope_type="system", reason="не админ")


def test_kill_switch_is_checked_again_at_claim_time(auto, admin):
    ctx, project = auto
    ctx.autopilot.tick(project)
    with ctx.db.session() as s:  # выключатель включили «в обход» перевода в удержание (гонка): отправка всё равно не произойдёт
        s.add(KillSwitch(project_id=project, scope_type="platform", scope_value="telegram", engaged_by="user:1", reason="гонка", engaged_at=ctx.clock.now()))
    ctx.clock.advance(days=1)
    ctx.publishing.run_due()
    with ctx.db.read() as s:
        tg = s.scalars(select(Publication).where(Publication.platform == "telegram")).first()
        fb = s.scalars(select(Publication).where(Publication.platform == "facebook")).first()
        assert tg.state == "awaiting_approval" and fb.state == "published"  # остановлена только платформа из зоны выключателя


def test_user_approved_publication_is_not_blocked_by_autopilot_kill_switch(ready_manual, admin):
    ctx, project, cpk = ready_manual
    with ctx.db.session() as s:
        ctx.killswitch.engage(s, admin, project_id=project, scope_type="project", reason="стоп автопилоту")
    with ctx.db.session() as s:
        pubs = ctx.publishing.create_for_content(s, project, cpk, admin, platforms=["telegram"])
        ctx.publishing.approve(s, project, pubs[0].id, admin)
    assert ctx.publishing.run_due()[0]["state"] == "published"  # явное решение человека — не автономная публикация


@pytest.fixture
def ready_manual(llm_ctx, corpus, pipeline, admin):
    ctx = llm_ctx
    pipeline(corpus)
    with ctx.db.read() as s:
        pid = s.scalars(select(Proposal).where(Proposal.slot == 1)).first().id
    cpk = ctx.content.create(corpus, pid, {}, admin)
    _accounts(ctx, corpus, admin, platforms=("telegram",), mode="manual")
    return ctx, corpus, cpk


def test_human_approval_makes_publication_manual_even_if_autopilot_created_it(auto, admin):
    """Решение человека — всегда ручное: не подпадает под выключатель автопилота и получает пометку «Проверено редактором»."""
    ctx, project = auto
    assert ctx.autopilot.tick(project)["status"] == "scheduled"
    with ctx.db.session() as s:
        ctx.killswitch.engage(s, admin, project_id=project, scope_type="project", reason="стоп")
    with ctx.db.read() as s:
        held = [p for p in s.scalars(select(Publication)) if p.origin == "autopilot"]
        assert held and {p.state for p in held} == {"awaiting_approval"} and all(p.approved_by is None for p in held)
        tg_id = next(p.id for p in held if p.platform == "telegram")
    with ctx.db.session() as s:
        ctx.publishing.approve(s, project, tg_id, admin, schedule={"mode": "now"})  # человек осознанно подтверждает, выключатель автопилота всё ещё включён
    res = {r["publication_id"]: r for r in ctx.publishing.run_due()}
    assert res[tg_id]["state"] == "published"
    with ctx.db.read() as s:
        pub = s.get(Publication, tg_id)
        assert pub.origin == "autopilot" and pub.approved_by == "user:1"
        assert s.scalars(select(Publication).where(Publication.platform == "facebook", Publication.origin == "autopilot")).first().state == "awaiting_approval"  # остальное остаётся на удержании
    from smi_agent.content.render import render_final
    from smi_agent.db.models import PlatformVersion
    from smi_agent.settings_model import load_project_settings

    with ctx.db.read() as s:
        v = s.get(PlatformVersion, pub.platform_version_id)
        cfg = load_project_settings(s.get(Project, project).settings)
        assert render_final("telegram", v, cfg, reviewed=True).disclosure.endswith("Проверено редактором.")
