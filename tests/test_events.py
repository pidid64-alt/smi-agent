import itertools
import random

from sqlalchemy import select

from smi_agent.db.models import Article, Event
from smi_agent.events.similarity import EventProf, make_idf, make_vec, pair_score
from smi_agent.ingestion.normalize import url_hash
from tests.conftest import add_article
from tests.fixtures.corpus import ARTICLES, SAME_EVENT, ns_article


def _groups(ctx, project):
    with ctx.db.read() as s:
        by_event: dict[int, set[str]] = {}
        for a in s.scalars(select(Article).where(Article.project_id == project)):
            aid = a.url.rsplit("/", 1)[-1]
            by_event.setdefault(a.event_id, set()).add(aid)
        return sorted((sorted(v) for v in by_event.values()), key=lambda v: v[0])


def test_similarity_thresholds_separate_same_event_from_same_topic(know):
    vecs = {a[0]: make_vec(ns_article(a[0], know), origin="g:" + a[1], group=a[1]) for a in ARTICLES}
    idf = make_idf([set(v.tf) for v in vecs.values()])
    same = {frozenset(p) for grp in SAME_EVENT for p in itertools.combinations(grp, 2)}
    for x, y in itertools.combinations(vecs, 2):
        prof = EventProf(0)
        prof.add(vecs[x])
        assert pair_score(vecs[y], prof, idf).attach == (frozenset((x, y)) in same), (x, y)


def test_corpus_clusters_into_expected_events(ctx, corpus):
    stats = ctx.events.process_new(corpus)
    assert stats.new_articles == len(ARTICLES) and stats.new_events == 6
    assert _groups(ctx, corpus) == [["A1", "A2", "A3", "A4", "A5"], ["B1"], ["C1", "C2", "C3"], ["D1", "D2"], ["E1"], ["F1"]]
    assert ctx.events.process_new(corpus).new_articles == 0  # повторный запуск ничего не меняет


def test_clustering_is_independent_of_arrival_order(ctx, project, load_corpus):
    ids = [a[0] for a in ARTICLES]
    random.Random(7).shuffle(ids)
    for chunk in (ids[:5], ids[5:9], ids[9:]):  # поступление батчами в произвольном порядке
        load_corpus(ctx, project, chunk)
        ctx.events.process_new(project)
    assert _groups(ctx, project) == [["A1", "A2", "A3", "A4", "A5"], ["B1"], ["C1", "C2", "C3"], ["D1", "D2"], ["E1"], ["F1"]]


def test_reprint_with_attribution_is_not_independent(ctx, corpus):
    ctx.events.process_new(corpus)
    with ctx.db.read() as s:
        arts = {a.url.rsplit("/", 1)[-1]: a for a in s.scalars(select(Article))}
        ev = s.get(Event, arts["A1"].event_id)
        assert arts["A5"].relation == "reprint" and not arts["A5"].independent  # «Об этом передает Kazinform»
        assert arts["A4"].independent and arts["A4"].relation == "independent"  # Reuters на английском — независимый материал
        assert arts["A1"].relation == "original"
        assert ev.n_articles == 5 and ev.n_independent == 4  # 5 публикаций ≠ 5 независимых источников
        assert ev.languages == ["en", "ru"]
        assert ev.geo == "kz" and ev.category in ("finance", "economy")


def test_many_reprints_of_one_origin_count_once(ctx, project):
    base = ("Национальный банк Казахстана сохранил базовую ставку на уровне 16,5% годовых. Инфляция в августе составила 12,3%. "
            "Решение принято на заседании Комитета по денежно-кредитной политике 30 сентября.")
    add_article(ctx, project, "nur_kz", "Нацбанк сохранил базовую ставку 16,5%", base, minutes=0)
    for i, src in enumerate(["zakon_kz", "informburo", "tengrinews", "kursiv"]):
        add_article(ctx, project, src, f"Ставка осталась 16,5%: Нацбанк #{i}", base + " Об этом передает NUR.KZ.", minutes=10 + i, cite=["nur_kz"])
    ctx.events.process_new(project)
    with ctx.db.read() as s:
        ev = s.scalar(select(Event))
        assert ev.n_articles == 5 and ev.n_independent == 1  # 4 перепечатки NUR.KZ не добавляют независимых источников


def test_cited_only_origin_counts_when_no_direct_article(ctx, project):
    body = "Компания опубликовала отчёт: выручка выросла на 18,4% до 3,2 млрд тенге за квартал. Об этом сообщает агентство."
    for i, src in enumerate(["zakon_kz", "informburo", "tengrinews"]):
        add_article(ctx, project, src, f"Выручка компании выросла на 18,4% #{i}", body, minutes=i, cite=["kazinform"])
    ctx.events.process_new(project)
    with ctx.db.read() as s:
        ev = s.scalar(select(Event))
        assert ev.n_articles == 3 and ev.n_independent == 1  # все три ссылаются на одно агентство → одно происхождение


def test_translation_joins_event_across_languages(ctx, corpus):
    ctx.events.process_new(corpus)
    with ctx.db.read() as s:
        arts = {a.url.rsplit("/", 1)[-1]: a for a in s.scalars(select(Article))}
        assert arts["C1"].event_id == arts["C2"].event_id == arts["C3"].event_id  # en + ru об одном событии


def test_event_features_are_aggregated_for_verification(ctx, corpus):
    ctx.events.process_new(corpus)
    with ctx.db.read() as s:
        ev = s.scalar(select(Event).where(Event.title.like("%ставк%")).limit(1))
        f = ev.features
        assert any(abs(n["v"] - 16.5) < 1e-6 and len(n["sources"]) >= 3 for n in f["numbers"])
        assert any(len(q["sources"]) >= 2 for q in f["quotes"])
        assert f["facts"] and f["carriers"] and "2026-09-30" in " ".join(f["dates"]) or f["dates"]
        assert ev.flags["hedge_share"] == 0 and ev.flags["official_present"] is False


def test_timeline_is_monotonic(ctx, corpus):
    ctx.events.process_new(corpus)
    with ctx.db.read() as s:
        ev = s.scalar(select(Event).where(Event.n_articles == 5))
        tl = ctx.events.timeline(s, ev)
        assert [p["independent"] for p in tl] == sorted(p["independent"] for p in tl) and tl[-1]["independent"] == 4
        d = ctx.events.detail(s, corpus, ev.id)
        assert len(d["articles"]) == 5


def test_url_hash_dedup_unique_constraint(ctx, project):
    add_article(ctx, project, "nur_kz", "Заголовок", "Текст " * 30, minutes=0, url="https://news.example/same")
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        add_article(ctx, project, "nur_kz", "Заголовок 2", "Текст " * 30, minutes=1, url="https://news.example/same?utm_source=x")
    assert url_hash("https://news.example/same") == url_hash("https://news.example/same?utm_source=x")
