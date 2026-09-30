from datetime import timedelta

import pytest
from sqlalchemy import select

from smi_agent.core.errors import ValidationFailed
from smi_agent.db.models import Content, Event, ProfileFeature
from smi_agent.learning.service import HALF_LIFE_DAYS, decay_factor
from smi_agent.profile.service import posterior


def _ev(category="finance", geo="kz", subs=("banking",)):
    e = Event(title="t", category=category, geo=geo, subtopics=list(subs), summary="")
    e.id = 1
    return e


def test_posterior_and_decay():
    assert posterior(0, 0) == 0.5 and posterior(5, 0) > 0.8 and posterior(0, 5) < 0.2
    assert decay_factor(None, None) == 1.0
    from datetime import UTC, datetime

    now = datetime(2026, 9, 30, tzinfo=UTC)
    assert decay_factor(now - timedelta(days=HALF_LIFE_DAYS), now) == pytest.approx(0.5)


def test_selections_raise_and_rejections_lower_affinity(ctx, project):
    fin, auto = _ev("finance", "kz", ("banking",)), _ev("auto", "world", ("electric_vehicles",))
    with ctx.db.session() as s:
        aud0, _, note0 = ctx.profile.affinity(s, project, fin)
        assert aud0 == 0.5 and "недостаточно данных" in note0  # холодный старт — нейтрально
        for _ in range(6):
            ctx.learning.apply(s, project, [(d, k, w, 0.0) for d, k, w in __import__("smi_agent.learning.service", fromlist=["event_features"]).event_features(fin)], source="user_action", signal="select")
            ctx.learning.apply(s, project, [(d, k, 0.0, w) for d, k, w in __import__("smi_agent.learning.service", fromlist=["event_features"]).event_features(auto)], source="user_action", signal="reject")
    with ctx.db.read() as s:
        a_fin, _, n_fin = ctx.profile.affinity(s, project, fin)
        a_auto, _, n_auto = ctx.profile.affinity(s, project, auto)
        assert a_fin > 0.65 > 0.35 > a_auto and "нравится" in n_fin and "реже" in n_auto


def test_affinity_feeds_trend_score_and_old_signals_fade(ctx, project):
    from smi_agent.learning.service import event_features

    fin = _ev("finance", "kz", ("banking",))
    with ctx.db.session() as s:
        for _ in range(8):
            ctx.learning.apply(s, project, [(d, k, w, 0.0) for d, k, w in event_features(fin)], source="user_action", signal="select")
    with ctx.db.read() as s:
        fresh, _, _ = ctx.profile.affinity(s, project, fin)
    ctx.clock.advance(days=HALF_LIFE_DAYS * 3)
    with ctx.db.read() as s:
        faded, _, _ = ctx.profile.affinity(s, project, fin)
    assert fresh > faded > 0.5  # давние сигналы затухают (интересы меняются), но не исчезают мгновенно


def test_edit_learning_and_style_hints(ctx, project, admin):
    c = Content(content_id="Content-2026-000999", project_id=project, title="t", category="finance", geo="kz", language="ru", created_at=ctx.clock.now(), updated_at=ctx.clock.now())
    with ctx.db.session() as s:
        s.add(c)
        s.flush()
        long_text = "слово " * 100 + "#a #b #c ! ! !"
        for _ in range(3):
            st = ctx.learning.record_edit(s, project, c, "telegram", "body", long_text, "слово " * 30, user_id=1)
        assert st["ratio"] < 0.5 and st["hashtags_before"] == 3 and st["hashtags_after"] == 0
        hints = ctx.profile.style_hints(s, project)
        assert hints["length_multiplier"] < 0.95 and hints["fewer_hashtags"] and hints["calmer_tone"]
        assert ctx.learning.record_edit(s, project, c, "telegram", "body", "одинаково", "одинаково")["changed"] is False


def test_performance_feedback_updates_historical_component(ctx, project):
    import math

    c = Content(content_id="Content-2026-000998", project_id=project, title="t", category="finance", geo="kz", subtopics=["banking"], language="ru", created_at=ctx.clock.now(), updated_at=ctx.clock.now())
    with ctx.db.session() as s:
        s.add(c)
        s.flush()
        for _ in range(6):
            ctx.learning.record_performance(s, project, None, c, math.log(2.0))  # в два раза лучше базового уровня
    with ctx.db.read() as s:
        _aud, hist, _n = ctx.profile.affinity(s, project, _ev("finance", "kz", ("banking",)))
        assert hist > 0.6
        _a2, hist_other, _ = ctx.profile.affinity(s, project, _ev("sport", "world", ("x",)))
        assert hist_other == 0.5


def test_profile_settings_validation_and_political_policy(ctx, project, admin):
    with ctx.db.session() as s:
        out = ctx.profile.update_settings(s, project, admin, {"tone": "formal", "priority_categories": ["economy"], "blocked_keywords": ["казино"]})
        assert out["tone"] == "formal" and out["political_policy"] == "confirm_only"
        for bad in ({"tone": "angry"}, {"political_policy": "auto"}, {"unknown": 1}, {"blocked_keywords": "казино"}):
            with pytest.raises(ValidationFailed):
                ctx.profile.update_settings(s, project, admin, bad)


def test_profile_snapshot_explains_what_the_system_understood(ctx, corpus, pipeline, admin):
    pipeline(corpus)
    snap0 = None
    with ctx.db.read() as s:
        snap0 = ctx.profile.snapshot(s, corpus)
    assert snap0["signals"] == 0 and "слишком мало сигналов" in snap0["statements"][0]
    for cmd in ("№1 неинтересна", "№2 неинтересна"):
        ctx.interaction.handle_text(corpus, admin, cmd)
    with ctx.db.read() as s:
        snap = ctx.profile.snapshot(s, corpus)
        assert snap["signals"] == 2 and snap["preferences"]["categories"] and snap["choice_analysis"]["proposals_shown"] >= 4
        assert any(c["rejected"] >= 1 for c in snap["choice_analysis"]["categories"])
