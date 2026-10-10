"""Демо-режим: вымышленные данные, сквозной сценарий и автопилот в песочнице."""

import pytest
from sqlalchemy import select

from smi_agent.config import Settings
from smi_agent.container import Container
from smi_agent.db.models import (
    Content,
    Event,
    FunnelItem,
    LearningEvent,
    Project,
    Proposal,
    Publication,
    UserAction,
)
from smi_agent.demo.seed import seed_demo
from smi_agent.security.rbac import Actor


@pytest.fixture
def demo(tmpdir_path):
    st = Settings(env="dev", demo_mode=True, data_dir=tmpdir_path, config_dir=tmpdir_path / "config", database_url=f"sqlite:///{tmpdir_path}/demo.db", backup_dir=tmpdir_path / "bk")
    ctx = Container(st)
    ctx.db.create_all()
    info = seed_demo(ctx)
    yield ctx, info["project_id"]
    ctx.close()


def test_seed_creates_fictional_world_and_five_diverse_proposals(demo):
    ctx, pid = demo
    with ctx.db.read() as s:
        assert s.query(Project).one().name.startswith("Демо-редакция")
        props = list(s.scalars(select(Proposal).where(Proposal.run_id.is_not(None)).order_by(Proposal.slot)))
        assert len(props) == 5 and props[0].card["trend_score"] >= props[-1].card["trend_score"]
        assert len({p.card["category"] for p in props}) >= 4  # разнообразие направлений
        assert all(p.card["verification"]["status"] in ("confirmed", "multi_confirmed") for p in props)
        assert {"kz", "world"} <= {p.card["geo"] for p in props}  # и казахстанская, и мировая повестка
        from smi_agent.db.models import Article

        assert all(a.url.startswith("https://demo.invalid/") for a in s.scalars(select(Article)))  # ссылки заведомо нерабочие: настоящих СМИ не задействовано


def test_raw_article_count_is_not_significance(demo):
    ctx, pid = demo
    with ctx.db.read() as s:
        storm = s.scalars(select(Event).where(Event.n_articles >= 30)).one()
        assert storm.n_articles == 40 and storm.n_independent == 7  # 40 публикаций, но лишь 7 независимых источников
        top = s.scalars(select(Event).where(Event.stage != "published").order_by(Event.trend_score.desc())).first()
        assert top.n_articles < storm.n_articles and top.trend_score > storm.trend_score  # выше — тема с меньшим числом публикаций, но с практической значимостью и первичным источником


def test_noise_is_filtered_with_reasons(demo):
    ctx, pid = demo
    with ctx.db.read() as s:
        titles = " ".join(p.card["title"] for p in s.scalars(select(Proposal).where(Proposal.run_id.is_not(None))))
        assert "ШОК" not in titles and "промокод" not in titles.lower() and "Якобы" not in titles
        dropped = {(s.get(Event, it.event_id).title[:12], " ".join(it.reasons)) for it in s.scalars(select(FunnelItem).where(FunnelItem.decision == "dropped"))}
        joined = " | ".join(f"{t}:{r}" for t, r in dropped)
        assert "Trend Score" in joined and "неподтверждённая информация" in joined  # кликбейт/реклама — ниже порога; слухи — отсеяны на этапе 2


def test_full_flow_with_scripted_texts_passes_all_checks_and_publishes_to_sandbox(demo):
    ctx, pid = demo
    admin = Actor.user(1, "admin")
    res = ctx.interaction.handle_text(pid, admin, "Беру №1")["results"][0]
    assert res["ok"], res["message"]
    with ctx.db.read() as s:
        c = s.get(Content, res["data"]["content_pk"])
        assert c.generator.startswith("llm:demo")
        d = ctx.content.to_dict(s, c)
        for v in d["versions"]:
            assert v["checks"]["passed"], [r for r in v["checks"]["results"] if r["status"] == "fail"]  # тексты проходят числа/цитаты/оригинальность
        assert {v["platform"] for v in d["versions"]} == {"telegram", "instagram", "facebook"}
    with ctx.db.session() as s:
        pubs = ctx.publishing.create_for_content(s, pid, res["data"]["content_pk"], admin)
        assert {p.state for p in pubs} == {"awaiting_approval"}
        for p in pubs:
            ctx.publishing.approve(s, pid, p.id, admin)
    assert {r["state"] for r in ctx.publishing.run_due()} == {"published"}
    with ctx.db.read() as s:
        assert all(p.external_id.startswith("sbx-") for p in s.scalars(select(Publication).where(Publication.origin == "user", Publication.external_id.like("sbx-%"))))
        assert s.query(UserAction).filter(UserAction.raw_text == "Беру №1").count() == 1


def test_autopilot_in_demo_publishes_only_safe_topics_and_respects_kill_switch(demo):
    ctx, pid = demo
    admin = Actor.user(1, "admin")
    from smi_agent.db.models import PlatformAccount

    with ctx.db.session() as s:
        for a in s.scalars(select(PlatformAccount).where(PlatformAccount.project_id == pid)):
            a.mode = "auto"
        ctx.autopilot.set_policy(s, admin, pid, enabled=True)
        ctx.autopilot.set_mode(s, admin, pid, "autopilot")
    from datetime import UTC, datetime

    from smi_agent.core.clock import FakeClock

    fake = FakeClock(datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0))  # 13:00 по Алматы — вне тихих часов
    ctx.clock = ctx.audit.clock = fake
    rec = ctx.autopilot.tick(pid)
    assert rec["status"] in ("scheduled", "nothing_suitable"), rec
    with ctx.db.read() as s:
        pubs = list(s.scalars(select(Publication).where(Publication.origin == "autopilot")))
        for p in pubs:
            c = s.get(Content, p.content_pk)
            ev = s.get(Event, c.event_id)
            assert not (ev.flags or {}).get("political") and not (ev.flags or {}).get("sensitive") and ev.verification_status == "multi_confirmed"
        if rec["status"] == "scheduled":
            assert pubs and all(p.state == "scheduled" and p.approved_by == "service:autopilot" for p in pubs)
    with ctx.db.session() as s:
        ctx.killswitch.engage(s, admin, project_id=pid, scope_type="project", reason="проверка")
    assert ctx.autopilot.tick(pid)["status"] == "killed"


def test_profile_and_analytics_reflect_seeded_history(demo):
    ctx, pid = demo
    with ctx.db.read() as s:
        snap = ctx.profile.snapshot(s, pid)
        assert snap["signals"] >= 24 and snap["statements"][0].startswith("Чаще выбираете")
        fa = ctx.metrics.forecast_accuracy(s, pid)
        assert fa["n"] == 24 and fa["correlation"] is not None
        ins = ctx.analytics.insights(s, pid)
        assert ins["findings"] and all(f["n"] >= 4 and f["confidence"] for f in ins["findings"])  # выводы — только при достаточной выборке
        assert s.query(LearningEvent).count() >= 24
        dash = ctx.analytics.dashboard(s, pid)
        assert len(dash["proposals"]) == 5 and dash["agenda"]["funnel"]["counts"]["s5"] == 5


def test_demo_is_refused_in_production(tmpdir_path):
    from pydantic import SecretStr

    from smi_agent.security.secrets import generate_key_entry

    st = Settings(env="production", demo_mode=True, data_dir=tmpdir_path, database_url=f"sqlite:///{tmpdir_path}/p.db", backup_dir=tmpdir_path / "b", master_keys=SecretStr(generate_key_entry()))
    ctx = Container(st)
    ctx.db.create_all()
    with pytest.raises(RuntimeError, match="production"):
        seed_demo(ctx)
    from fastapi.testclient import TestClient

    from smi_agent.api.app import create_app

    with pytest.raises(RuntimeError, match="production"):
        with TestClient(create_app(ctx)):
            pass
    assert ctx.llm.enabled is False  # демо-«LLM» в production не подключается
    ctx.close()
