import math
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from smi_agent.analytics.metrics import primary_value
from smi_agent.core.errors import ValidationFailed
from smi_agent.db.models import Content, ForecastRecord, LearningEvent, MetricSnapshot, PerformanceStat, PlatformVersion, Publication
from smi_agent.security.rbac import Actor


def _pub(ctx, ids, platform):
    with ctx.db.read() as s:
        return next(p.id for p in s.scalars(select(Publication).where(Publication.id.in_(ids))) if p.platform == platform)


def _snap(ctx, pub_id, age, **vals):
    with ctx.db.session() as s:
        pub = s.get(Publication, pub_id)
        base = {k: None for k in ("views", "reach", "likes", "comments", "shares", "saves", "clicks", "follows")}
        s.add(MetricSnapshot(publication_id=pub_id, captured_at=pub.published_at + timedelta(hours=age), age_hours=age, unavailable=[k for k, v in base.items() if k not in vals], source="import", extra={}, **{**base, **vals}))


def test_collect_records_unavailable_honestly_and_never_invents_numbers(published):
    ctx, project, cpk, ids = published
    ctx.clock.advance(hours=2)
    res = ctx.metrics.collect_due(project)
    assert len(res) == 3 and all("views" in r["unavailable"] for r in res)
    with ctx.db.read() as s:
        snaps = list(s.scalars(select(MetricSnapshot)))
        assert snaps and all(x.views is None and x.likes is None for x in snaps)  # песочница метрик не даёт — NULL, а не нули
    ctx.clock.advance(minutes=10)
    assert ctx.metrics.collect_due(project) == []  # следующая контрольная точка ещё не наступила


def test_manual_import_validates_rows(published, admin):
    ctx, project, cpk, ids = published
    with ctx.db.session() as s:
        out = ctx.metrics.import_rows(s, project, admin, [
            {"publication_id": ids[0], "views": 1200, "likes": 45, "shares": 7},
            {"content_id": "Content-2026-000001", "platform": "instagram", "reach": 800, "saves": 12},
            {"publication_id": ids[0], "views": -5},
            {"publication_id": 9999, "views": 1},
            {"publication_id": ids[1], "views": "много"},
        ], source="manual")
        assert out["imported"] == 2 and len(out["errors"]) == 3
        st = ctx.metrics.post_stats(s, project, ids[0])
        assert st["latest"]["views"] == 1200 and st["latest"]["comments"] is None and "comments" in st["unavailable"]
        assert st["engagement_rate"] == pytest.approx(52 / 1200, abs=1e-4) and st["engagement_base"] == "views"  # охвата нет — считаем по просмотрам и говорим об этом
    with ctx.db.session() as s:
        with pytest.raises(ValidationFailed):
            ctx.metrics.import_rows(s, project, admin, [], source="hack")


def test_performance_feedback_needs_baseline_and_is_recorded_once(published, admin):
    ctx, project, cpk, ids = published
    tg = _pub(ctx, ids, "telegram")
    _snap(ctx, tg, 30, views=2000, likes=50)
    assert ctx.metrics.evaluate(project) == []  # базовый уровень не сформирован (нужно ≥5 постов с метриками)
    with ctx.db.read() as s:
        fr = s.scalars(select(ForecastRecord).where(ForecastRecord.publication_id == tg)).one()
        assert fr.evaluated_at is None and "базовый уровень не сформирован" in fr.actual["note"]
    # создаём историю: 6 опубликованных ранее постов в Telegram со стабильными ~1000 просмотров
    with ctx.db.session() as s:
        acc = s.get(Publication, tg).account_id
        c = s.get(Content, cpk)
        for i in range(6):
            p = Publication(project_id=project, content_pk=c.id, platform_version_id=s.get(Publication, tg).platform_version_id, platform="telegram", account_id=acc, version=1, state="published", idempotency_key=f"hist{i}", published_at=ctx.clock.now() - timedelta(days=3 + i), external_id=f"h{i}", origin="user", created_at=ctx.clock.now(), updated_at=ctx.clock.now())
            s.add(p)
            s.flush()
            s.add(MetricSnapshot(publication_id=p.id, captured_at=ctx.clock.now(), age_hours=48, views=1000 + i * 10, unavailable=[], source="import", extra={}))
    out = [r for r in ctx.metrics.evaluate(project) if r["publication_id"] == tg]
    assert len(out) == 1 and out[0]["ratio"] == pytest.approx(2000 / 1025, abs=0.05)
    with ctx.db.read() as s:
        fr = s.scalars(select(ForecastRecord).where(ForecastRecord.publication_id == tg)).one()
        assert fr.actual["ratio"] > 1.8 and fr.error["abs_error"] >= 0 and fr.evaluated_at
        n_perf = s.query(LearningEvent).filter(LearningEvent.source == "performance").count()
        assert n_perf == 7 and s.query(PerformanceStat).filter(PerformanceStat.dimension == "category", PerformanceStat.key == "finance").one().n == 7
    assert ctx.metrics.evaluate(project) == []  # повторная оценка не удваивает сигнал
    with ctx.db.read() as s:
        assert s.query(LearningEvent).filter(LearningEvent.source == "performance").count() == n_perf


def test_primary_value_priority():
    class M:
        def __init__(self, **k):
            self.views = self.reach = self.likes = self.comments = self.shares = self.saves = None
            self.__dict__.update(k)

    assert primary_value(M(views=10, reach=99)) == 10 and primary_value(M(reach=99)) == 99
    assert primary_value(M(likes=10, comments=2, shares=1, saves=3)) == 10 + 4 + 3 + 3 and primary_value(M()) is None


def test_calibration_learns_from_outcomes_and_shrinks_with_few_samples(ctx, project):
    base_slope, _i, n0 = ctx.metrics.calibration(ctx.db.read().__enter__(), project)
    assert (base_slope, n0) == (0.5, 0)
    with ctx.db.session() as s:
        for i in range(12):  # интерес 0.2..0.9 → реальный эффект сильнее заложенного приора (наклон ≈ 1.6)
            interest = 0.2 + i * 0.065
            s.add(ForecastRecord(project_id=project, content_pk=i + 1, category="finance", prediction={"interest": interest}, actual={"log_ratio": 1.6 * (interest - 0.5) * 2 / 2 * 1.0}, error={"predicted_log_ratio": 0, "abs_error": 0.1, "signed": 0.1}, created_at=ctx.clock.now(), evaluated_at=ctx.clock.now()))
    with ctx.db.read() as s:
        slope, _i, n = ctx.metrics.calibration(s, project)
        assert n == 12 and slope > 0.5 + 0.1  # данные сдвинули прогноз от приора
        acc = ctx.metrics.forecast_accuracy(s, project)
        assert acc["n"] == 12 and acc["correlation"] > 0.9 and acc["mae_log"] == pytest.approx(0.1)
        fc = ctx.metrics.forecast_for(s, project, {"interest": 0.9})
        assert fc["expected_ratio"] > 1 and fc["confidence"] == "средняя" and fc["calibration_samples"] == 12
        assert ctx.metrics.forecast_for(s, project, {"interest": 0.1})["expected_ratio"] < 1


def test_series_periods_buckets_and_validation(published, admin):
    ctx, project, cpk, ids = published
    with ctx.db.read() as s:
        a = ctx.analytics
        today = a.series(s, project, "posts", "today")
        assert today["bucket"] == "hour" and len(today["labels"]) <= 24 and {x["name"] for x in today["series"]} == {"telegram", "instagram", "facebook"}
        d7 = a.series(s, project, "posts", "7d")
        assert d7["bucket"] == "day" and 7 <= len(d7["labels"]) <= 9 and sum(sum(x["points"]) for x in d7["series"]) == 3
        assert a.series(s, project, "posts", "3m")["bucket"] == "week" and a.series(s, project, "posts", "year")["bucket"] == "month"
        now = ctx.clock.now()
        custom = a.series(s, project, "posts", "custom", start=now - timedelta(days=10), end=now, platform="telegram")
        assert [x["name"] for x in custom["series"]] == ["telegram"]
        for bad in (dict(metric="nope", period="7d"), dict(metric="posts", period="decade"), dict(metric="posts", period="custom")):
            with pytest.raises(ValidationFailed):
                a.series(s, project, **bad)
        with pytest.raises(ValidationFailed):
            a.series(s, project, "posts", "custom", start=now, end=now - timedelta(days=1))
        views = a.series(s, project, "views", "7d")
        assert views["unavailable_platforms"] == ["facebook", "instagram", "telegram"]  # метрик нет — честно сообщаем, а не рисуем нули
        kz = a.series(s, project, "kz_share", "7d")
        assert {x["name"] for x in kz["series"]} == {"Доля КЗ", "Цель"} and kz["series"][1]["points"][0] == 0.6
        assert a.series(s, project, "events", "7d")["series"][0]["points"]
    _snap(ctx, _pub(ctx, ids, "telegram"), 26, views=500)
    with ctx.db.read() as s:
        v = ctx.analytics.series(s, project, "views", "7d", platform="telegram")
        assert sum(v["series"][0]["points"]) == 500


def test_geo_ratio_uses_actual_published_content(published):
    ctx, project, cpk, ids = published
    now = ctx.clock.now()
    with ctx.db.read() as s:
        g = ctx.analytics.geo_ratio(s, project, now - timedelta(days=7), now + timedelta(days=1))
        assert g["target"] == {"kz": 0.6, "world": 0.4} and g["published"] == {"kz": 1, "world": 0, "kz_share": 1.0}
        assert g["proposed"]["kz"] + g["proposed"]["world"] >= 4 and "ориентир" in g["note"]


def test_insights_require_enough_data_then_find_real_patterns(ctx, project):
    with ctx.db.read() as s:
        none = ctx.analytics.insights(s, project)
        assert none["findings"] == [] and "Недостаточно данных" in none["note"]
    with ctx.db.session() as s:
        for i in range(16):
            cat = "finance" if i % 2 == 0 else "sport"
            c = Content(content_id=f"Content-2026-9{i:05d}", project_id=project, title="t", category=cat, geo="kz", subtopics=[], language="ru", draft={"format_code": "post_card"}, fact_base=[], created_at=ctx.clock.now(), updated_at=ctx.clock.now())
            s.add(c)
            s.flush()
            v = PlatformVersion(content_pk=c.id, platform="telegram", version=1, is_current=True, format="post", title="t", body="b", language="ru", created_at=ctx.clock.now())
            s.add(v)
            s.flush()
            p = Publication(project_id=project, content_pk=c.id, platform_version_id=v.id, platform="telegram", version=1, state="published", idempotency_key=f"ins{i}", published_at=ctx.clock.now() - timedelta(days=i), origin="user", created_at=ctx.clock.now(), updated_at=ctx.clock.now())
            s.add(p)
            s.add(ForecastRecord(project_id=project, content_pk=c.id, category=cat, prediction={"interest": 0.5}, actual={"log_ratio": 0.6 + (i % 3) * 0.02 if cat == "finance" else -0.5 + (i % 3) * 0.02}, error={"abs_error": 0.1, "signed": 0, "predicted_log_ratio": 0}, created_at=ctx.clock.now(), evaluated_at=ctx.clock.now()))
    with ctx.db.read() as s:
        res = ctx.analytics.insights(s, project)
        cats = {f["key"]: f for f in res["findings"] if f["dimension"] == "category"}
        assert cats["finance"]["effect"] > 0 and cats["sport"]["effect"] < 0 and cats["finance"]["n"] == 8
        assert "не доказанные причины" in res["note"]


def test_weekly_report_and_recommendations(published, admin):
    ctx, project, cpk, ids = published
    with ctx.db.session() as s:
        rep = ctx.analytics.build_report(s, project, "weekly")
        assert rep.kind == "weekly" and rep.payload["publications"]["total"] == 3 and rep.payload["geo"]["published"]["kz"] == 1
        for section in ("# Недельный отчёт", "## Публикации", "## Гео-баланс", "## Воронка", "## Предложения и выбор пользователя", "## Прогноз и факт", "## Рекомендации"):
            assert section in rep.markdown
        assert rep.payload["recommendations"]
        for kind in ("monthly", "strategy"):
            assert ctx.analytics.build_report(s, project, kind).kind == kind
        with pytest.raises(ValidationFailed):
            ctx.analytics.build_report(s, project, "daily")


def test_dashboard_has_all_six_sections(published):
    ctx, project, cpk, ids = published
    with ctx.db.read() as s:
        d = ctx.analytics.dashboard(s, project)
    assert {"agenda", "proposals", "content", "analytics", "profile", "publications"} <= set(d)  # шесть разделов ТЗ §43
    assert d["agenda"]["events"] and d["agenda"]["funnel"]["counts"]["s5"] >= 1 and d["agenda"]["sources"]
    assert len(d["publications"]) == 3 and d["content"][0]["content_id"] == "Content-2026-000001" and d["mode"] == "learning"
    assert d["analytics"]["geo"]["published"]["kz"] == 1
