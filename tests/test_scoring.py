from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from smi_agent.db.models import Event
from smi_agent.scoring.trend import classify_phase, sat, velocity_windows
from smi_agent.settings_model import DEFAULT_WEIGHTS, load_project_settings

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _event(ctx, **over):
    """Событие в памяти (без БД) для чистой проверки формулы."""
    carriers = over.pop("carriers", [])
    ev = Event(
        project_id=1, title=over.pop("title", "Нацбанк сохранил базовую ставку на уровне 16,5%"), summary="Решение принято", first_published_at=over.pop("first", NOW - timedelta(hours=2)), last_update_at=over.pop("last", NOW - timedelta(minutes=20)),
        first_seen_at=NOW, geo="kz", geo_bucket="KZ", category="finance", subtopics=["banking"], n_articles=over.pop("n_articles", len(carriers)), n_sources=len(carriers), n_independent=over.pop("n_ind", len(carriers)),
        kz_relevance=over.pop("kz", 0.8), world_relevance=over.pop("world", 0.1), flags=over.pop("flags", {}),
        features={"carriers": carriers, "facts": [{"text": "a"}, {"text": "b"}, {"text": "c"}], "numbers": [{"v": 16.5, "u": "pct"}], "dates": ["09-30"], "quotes": []},
    )  # fmt: skip
    assert not over, over
    return ev


def _carriers(minutes_ago: list[int], rel: float = 0.8):
    return [{"t": (NOW - timedelta(minutes=m)).isoformat(), "r": rel, "tier": "quality", "c": "KZ", "k": f"s{i}"} for i, m in enumerate(minutes_ago)]


def test_velocity_windows_match_spec_example():
    # ТЗ: 08:00 → 3 источника, 10:00 → 15, 12:00 → 40 = «быстро набирает популярность»
    t = lambda h, n: [datetime(2026, 9, 30, h, tzinfo=UTC) - timedelta(minutes=i) for i in range(n)]  # noqa: E731
    times = t(8, 3) + t(10, 12) + t(12, 25)
    v_now, v_prev, n_now, n_prev = velocity_windows(times, datetime(2026, 9, 30, 12, tzinfo=UTC), 2.0)
    assert (n_now, n_prev) == (25, 12) and v_now > v_prev > 0
    assert classify_phase(age_first_h=4, age_last_h=0, n_ind=40, v_now=v_now, v_prev=v_prev, max_age_h=72, freshness=0.9) == "rising"


def test_phase_classifier():
    kw = dict(max_age_h=72)
    assert classify_phase(age_first_h=1, age_last_h=0, n_ind=1, v_now=0, v_prev=0, freshness=0.95, **kw) == "emerging"
    assert classify_phase(age_first_h=90, age_last_h=10, n_ind=9, v_now=0, v_prev=0, freshness=0.5, **kw) == "stale"
    assert classify_phase(age_first_h=10, age_last_h=0, n_ind=8, v_now=2.0, v_prev=0.5, freshness=0.8, **kw) == "rising"
    assert classify_phase(age_first_h=12, age_last_h=1, n_ind=10, v_now=0.5, v_prev=0.6, freshness=0.7, **kw) == "peak"
    assert classify_phase(age_first_h=20, age_last_h=5, n_ind=10, v_now=0.1, v_prev=1.5, freshness=0.4, **kw) == "fading"


def test_score_is_weighted_sum_of_thirteen_components(ctx):
    cfg = load_project_settings({})
    ev = _event(ctx, carriers=_carriers([100, 80, 50, 30, 10]))
    res = ctx.scoring.score_event(ev, cfg, NOW)
    v = res.values
    expect = sum(DEFAULT_WEIGHTS[k] * v[k] for k in DEFAULT_WEIGHTS) / sum(DEFAULT_WEIGHTS.values()) * 100
    assert res.score == pytest.approx(expect, abs=0.01)
    assert set(DEFAULT_WEIGHTS) <= set(v) and all(0 <= x <= 1 for x in v.values())


def test_velocity_counts_only_independent_sources_and_needs_more_than_one(ctx):
    cfg = load_project_settings({})
    solo = ctx.scoring.score_event(_event(ctx, carriers=_carriers([10]), n_articles=40, n_ind=1), cfg, NOW)
    assert solo.values["velocity"] == 0.0  # 40 перепечаток одного источника ≠ скорость роста
    many = ctx.scoring.score_event(_event(ctx, carriers=_carriers([5, 15, 25, 40, 60, 90]), n_ind=6), cfg, NOW)
    assert many.values["velocity"] > 0.6 and many.velocity > solo.velocity
    assert many.score > solo.score


def test_more_independent_sources_raise_score_more_than_raw_article_count(ctx):
    cfg = load_project_settings({})
    reprints = ctx.scoring.score_event(_event(ctx, carriers=_carriers([30]), n_articles=50, n_ind=1), cfg, NOW)
    independent = ctx.scoring.score_event(_event(ctx, carriers=_carriers([30, 40, 50, 60, 70]), n_articles=5, n_ind=5), cfg, NOW)
    assert independent.score > reprints.score + 5


def test_penalties_reduce_score(ctx):
    cfg = load_project_settings({})
    base = ctx.scoring.score_event(_event(ctx, carriers=_carriers([20, 30, 40])), cfg, NOW)
    bad = ctx.scoring.score_event(_event(ctx, carriers=_carriers([20, 30, 40]), flags={"clickbait_share": 1.0, "hedge_share": 0.8, "ad_share": 0.5, "injection": True}), cfg, NOW)
    assert bad.score < base.score * 0.4 and {"clickbait", "hedge", "ad", "injection"} <= set(bad.penalties)


def test_freshness_decays_with_age(ctx):
    cfg = load_project_settings({})
    fresh = ctx.scoring.score_event(_event(ctx, carriers=_carriers([30]), first=NOW - timedelta(hours=1), last=NOW - timedelta(minutes=30)), cfg, NOW)
    old = ctx.scoring.score_event(_event(ctx, carriers=_carriers([40 * 60, 39 * 60, 38 * 60]), first=NOW - timedelta(hours=40), last=NOW - timedelta(hours=38)), cfg, NOW)
    assert fresh.values["freshness"] > 0.85 > 0.3 > old.values["freshness"]
    assert old.phase in ("stale", "fading")


def test_weights_are_configurable_without_code_changes(ctx):
    w = {k: 0.0 for k in DEFAULT_WEIGHTS} | {"freshness": 1.0}
    cfg = load_project_settings({"scoring": {"weights": w}})
    ev = _event(ctx, carriers=_carriers([30]))
    res = ctx.scoring.score_event(ev, cfg, NOW)
    assert res.score == pytest.approx(res.values["freshness"] * 100, abs=0.01)


def test_geo_significance_takes_max_of_kz_and_world(ctx):
    cfg = load_project_settings({})
    kz = ctx.scoring.score_event(_event(ctx, carriers=_carriers([30, 40]), kz=0.9, world=0.0), cfg, NOW)
    world = ctx.scoring.score_event(_event(ctx, carriers=_carriers([30, 40]), kz=0.0, world=0.9), cfg, NOW)
    assert kz.values["significance"] == pytest.approx(max(kz.values["kz_significance"], kz.values["world_significance"] * 0.92), abs=0.2)
    assert kz.values["kz_significance"] > kz.values["world_significance"] and world.values["world_significance"] > world.values["kz_significance"]


def test_scored_events_in_db(ctx, corpus):
    ctx.events.process_new(corpus)
    with ctx.db.session() as s:
        assert ctx.scoring.score_recent(s, corpus) == 6
    with ctx.db.read() as s:
        top = s.scalars(select(Event).order_by(Event.trend_score.desc())).first()
        assert top.n_independent == 4 and top.components["explain"]["velocity"] and 0 < top.trend_score <= 100
        single = s.scalar(select(Event).where(Event.n_articles == 1, Event.category == "unusual"))
        assert single.trend_score < top.trend_score


def test_sat_monotonic():
    assert sat(0, 3) == 0 and sat(3, 3) < sat(6, 3) < 1
