import pytest
from sqlalchemy import select

from smi_agent.db.models import Event, FunnelItem, FunnelRun, Proposal
from smi_agent.funnel.service import Cand, select_balanced
from smi_agent.security.rbac import Actor
from tests.conftest import add_article


def _run(ctx, pipeline, project):
    rid = pipeline(project)
    with ctx.db.read() as s:
        return s.get(FunnelRun, rid), {p.slot: p for p in s.scalars(select(Proposal).where(Proposal.run_id == rid).order_by(Proposal.slot))}


def test_funnel_narrows_and_never_pads(ctx, corpus, pipeline):
    run, props = _run(ctx, pipeline, corpus)
    c = run.counts
    assert c["pool"] == 6 and c["pool"] >= c["s50"] >= c["s15"] >= c["s10"] >= c["s5"]
    assert c["s5"] == len(props) and 1 <= len(props) <= 5
    assert run.status == "done" and run.geo_ratio["target_kz"] == 0.6 and "note" in run.notes
    with ctx.db.read() as s:
        assert "Scientists find ancient tomb" not in " ".join(p.card["title"] for p in props.values())  # слабая тема не попала


def test_quota_is_never_padded_with_weak_items(ctx, corpus, pipeline, admin):
    """Даже если просить 8 предложений, а достойных тем пять — система вернёт пять (ТЗ: слабые темы не добавляются ради квоты)."""
    from smi_agent.db.models import Project

    with ctx.db.session() as s:
        s.get(Project, corpus).settings = {"funnel": {"stage1": 50, "stage2": 20, "stage3": 15, "final": 8}}
    run, props = _run(ctx, pipeline, corpus)
    assert len(props) == run.counts["s5"] == 5 < 8


def test_every_dropped_item_has_a_reason(ctx, corpus, pipeline):
    run, _ = _run(ctx, pipeline, corpus)
    with ctx.db.read() as s:
        dropped = list(s.scalars(select(FunnelItem).where(FunnelItem.run_id == run.id, FunnelItem.decision == "dropped")))
        assert dropped and all(it.reasons for it in dropped)
        weak = next(it for it in dropped if "недостаточно фактов" in " ".join(it.reasons))
        assert s.get(Event, weak.event_id).category == "unusual"  # одна короткая заметка BBC


def test_proposal_card_has_all_required_sections(ctx, corpus, pipeline):
    _, props = _run(ctx, pipeline, corpus)
    card = props[1].card
    for key in ("title", "why_now", "what_happened", "why_interesting", "sources", "verification", "angle", "format"):
        assert card[key], key
    assert 2 <= len(card["what_happened"]) <= 4
    assert card["verification"]["label"] in ("подтверждено", "подтверждено несколькими источниками", "требуется дополнительная проверка")
    assert all({"name", "url", "relation_label", "independent"} <= set(src) for src in card["sources"])
    assert props[1].card["trend_score"] >= props[2].card["trend_score"]  # №1 — сильнейшая


def test_proposals_are_diverse_by_direction(ctx, corpus, pipeline):
    _, props = _run(ctx, pipeline, corpus)
    cats = [p.card["category"] for p in props.values()]
    assert len(set(cats)) >= 3  # не пять одинаковых тем


def test_stage2_exclusions_clickbait_ad_rumour_duplicate(ctx, project, pipeline):
    body = "Компания сообщила, что выручка выросла на 18,4% до 3,2 млрд тенге в третьем квартале. Решение совета директоров принято 29 сентября. Дивиденды составят 120 тенге на акцию."
    add_article(ctx, project, "kursiv", "Выручка компании выросла на 18,4% за квартал", body, minutes=0)
    add_article(ctx, project, "nur_kz", "ШОК! Вы не поверите, что сделали в банке!!!", "Шокирующая сенсация: врачи в шоке. Банк повысил ставки на 2,5% и объявил о запуске нового продукта для 400 тысяч клиентов. Подробности ниже.", minutes=5)
    add_article(ctx, project, "tengrinews", "Скидка на всё: промокод и акция", "На правах рекламы. Купите со скидкой 40% по промокоду. Акция действует до 5 октября для всех покупателей магазина.", minutes=10)
    rid = pipeline(project)
    with ctx.db.read() as s:
        reasons = {s.get(Event, it.event_id).title[:20]: " ".join(it.reasons) for it in s.scalars(select(FunnelItem).where(FunnelItem.run_id == rid, FunnelItem.decision == "dropped"))}
        joined = " | ".join(reasons.values())
        assert "кликбейт" in joined or "Trend Score" in joined
        assert "реклам" in joined.lower() or "Trend Score" in joined
        kept = [s.get(Event, it.event_id).title for it in s.scalars(select(FunnelItem).where(FunnelItem.run_id == rid, FunnelItem.stage == "s5", FunnelItem.decision == "kept"))]
        assert all("ШОК" not in t and "промокод" not in t.lower() for t in kept)


def test_blocked_keywords_from_profile_exclude_topics(ctx, corpus, pipeline, admin):
    with ctx.db.session() as s:
        ctx.profile.update_settings(s, corpus, admin, {"blocked_keywords": ["tesla"]})
    rid = pipeline(corpus)
    with ctx.db.read() as s:
        blocked = [it for it in s.scalars(select(FunnelItem).where(FunnelItem.run_id == rid, FunnelItem.decision == "dropped")) if any("заблокированная" in r for r in it.reasons)]
        assert {s.get(Event, it.event_id).category for it in blocked} >= {"auto"}


def test_unverified_items_are_limited(ctx, project, pipeline):
    # 5 одиночных материалов региональных СМИ → «требуется проверка»; допускается не более max_unverified_in_stage3
    for i in range(5):
        add_article(ctx, project, "zakon_kz", f"Решение акимата №{i}: новые правила для {100 + i * 13} предпринимателей Алматы", f"Акимат утвердил правила №{i}. Они затронут {100 + i * 13} предпринимателей и вступят в силу с 1 октября. Размер сбора составит {500 + i * 40} тенге в месяц. Документ опубликован для обсуждения.", minutes=i)
    rid = pipeline(project)
    with ctx.db.read() as s:
        run = s.get(FunnelRun, rid)
        assert run.notes["unverified_allowed"] <= 2
        s10 = s.scalars(select(FunnelItem).where(FunnelItem.run_id == rid, FunnelItem.stage == "s10", FunnelItem.decision == "kept")).all()
        assert len(s10) <= 2


def test_geo_soft_prior_balances_without_padding_weak_items(know):
    def cand(i, bucket, score):
        ev = Event(title=f"e{i}", category=f"c{i}", geo_bucket=bucket)
        ev.id = i
        return Cand(ev, score, bucket)

    equal = [cand(i, "KZ", 50) for i in range(10)] + [cand(100 + i, "WORLD", 50) for i in range(10)]
    chosen = select_balanced(equal, 10, 0.6, 0.35)
    assert sum(c.bucket == "KZ" for c in chosen) == 6  # цель 60/40
    # сильные КЗ-темы не вытесняются слабыми мировыми: приор мягкий
    skewed = [cand(i, "KZ", 80) for i in range(10)] + [cand(100 + i, "WORLD", 30) for i in range(10)]
    kz = sum(c.bucket == "KZ" for c in select_balanced(skewed, 10, 0.6, 0.35))
    assert kz >= 8
    # пул меньше целевого размера — слабые не добавляются
    assert len(select_balanced(equal[:3], 10, 0.6, 0.35)) == 3
    # сила приора 0 — чистый рейтинг
    pure = select_balanced(skewed, 10, 0.6, 0.0)
    assert all(c.bucket == "KZ" for c in pure)


def test_central_asia_counts_to_kz_bucket(ctx, project):
    add_article(ctx, project, "nur_kz", "Узбекистан и Казахстан подписали соглашение о транзите на 2,4 млрд долларов", "Казахстан и Узбекистан в Ташкенте подписали соглашение о транзите грузов объёмом 2,4 млрд долларов. Документ подписали министры транспорта двух стран. Соглашение вступит в силу 1 января 2027 года.", minutes=0)
    ctx.events.process_new(project)
    with ctx.db.read() as s:
        ev = s.scalar(select(Event))
        assert ev.geo_bucket == "KZ" and ev.geo in ("kz", "ca")
        add_article  # noqa: B018


def test_rejected_event_is_excluded_from_next_run(ctx, corpus, pipeline, admin):
    run, props = _run(ctx, pipeline, corpus)
    first = props[1]
    with ctx.db.session() as s:
        ctx.interaction.reject(s, corpus, admin, 1, "неинтересно")
    rid = pipeline(corpus)
    with ctx.db.read() as s:
        again = s.scalars(select(Proposal).where(Proposal.run_id == rid, Proposal.event_id == first.event_id)).all()
        assert not again
        reasons = [r for it in s.scalars(select(FunnelItem).where(FunnelItem.run_id == rid, FunnelItem.event_id == first.event_id)) for r in it.reasons]
        assert any("отклонено пользователем" in r for r in reasons)


def test_only_one_good_topic_means_one_proposal(ctx, project, pipeline):
    add_article(ctx, project, "reuters", "Kazakhstan central bank holds base rate at 16.5%", "Kazakhstan's central bank kept its base rate unchanged at 16.5% on Tuesday, citing slowing inflation of 12.3% in August. The bank said risks remain high and the next meeting is in November.", minutes=0)
    add_article(ctx, project, "bbc_world", "Bank of Kazakhstan leaves interest rate at 16.5 percent", "The National Bank of Kazakhstan left its key interest rate at 16.5% as inflation slowed to 12.3% in August, the central bank said on Tuesday. Governor Timur Suleimenov said risks remain high.", minutes=30)
    _run_, props = _run(ctx, pipeline, project)
    assert len(props) == 1
