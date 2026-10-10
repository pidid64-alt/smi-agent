from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

os.environ.setdefault("SMI_SCRYPT_LOG_N", "10")  # быстрый scrypt в тестах

from smi_agent.config import Settings  # noqa: E402
from smi_agent.container import Container  # noqa: E402
from smi_agent.core.clock import FakeClock  # noqa: E402
from smi_agent.db.models import Article, Project, Source  # noqa: E402
from smi_agent.ingestion.normalize import canonical_url, url_hash  # noqa: E402
from smi_agent.knowledge import get_knowledge  # noqa: E402
from smi_agent.security.rbac import Actor  # noqa: E402
from tests.fixtures.corpus import ARTICLES, T0, published_at, source_row  # noqa: E402


@pytest.fixture(scope="session")
def know():
    return get_knowledge()


@pytest.fixture
def tmpdir_path() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="smi-test-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def settings(tmpdir_path: Path) -> Settings:
    return Settings(env="test", data_dir=tmpdir_path, config_dir=tmpdir_path / "config", database_url=f"sqlite:///{tmpdir_path}/test.db", backup_dir=tmpdir_path / "backups", allow_private_fetch=False)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0 + timedelta(hours=4))


@pytest.fixture
def ctx(settings: Settings, clock: FakeClock) -> Iterator[Container]:
    c = Container(settings, clock=clock)
    c.db.create_all()
    yield c
    c.close()


@pytest.fixture
def admin() -> Actor:
    return Actor.user(1, "admin")


@pytest.fixture
def user_actor() -> Actor:
    return Actor.user(2, "user")


@pytest.fixture
def project(ctx: Container, admin: Actor) -> int:
    with ctx.db.session() as s:
        p = Project(slug="demo", name="Тестовый проект", settings={})
        s.add(p)
        s.flush()
        ctx.ingest.seed_sources(s, p.id, admin)
        return p.id


def _load_corpus(ctx: Container, project_id: int, ids: list[str] | None = None) -> None:
    """Кладёт синтетические статьи в БД с признаками, как это делает сбор (без HTTP)."""
    from smi_agent.core.text import sha256_hex, simhash, tokenize
    from smi_agent.ingestion.features import build_alias_index, compute_features

    with ctx.db.session() as s:
        srcs = list(s.scalars(select(Source).where(Source.project_id == project_id)))
        by_key = {x.key: x for x in srcs}
        # служебный официальный источник для тестов первичного подтверждения
        if "nbk_official" not in by_key:
            row = source_row("nbk")
            x = Source(project_id=project_id, key=row["key"], name=row["name"], kind="rss", url="https://www.nationalbank.kz/rss", country="KZ", tier="primary", reliability=0.98, independence_group="nbk", is_official=True, languages=["ru"], aliases=["Нацбанк РК"], config={}, state={})
            s.add(x)
            s.flush()
            by_key["nbk_official"] = x
            srcs.append(x)
        alias = build_alias_index(srcs)
        for aid, src, minutes, title, body in ARTICLES:
            if ids and aid not in ids:
                continue
            row = source_row(src)
            f = compute_features(title, "", body, know=ctx.know, alias_index=alias).as_dict()
            toks = tokenize(f"{title} {body[:1000]}", lang=f["lang"])
            url = f"https://news.example/{aid}"
            s.add(Article(
                project_id=project_id, source_id=by_key[row["key"]].id, url=url, canonical_url=canonical_url(url), url_hash=url_hash(url), title=title, summary="", body=body, lang=f["lang"],
                published_at=published_at(minutes), fetched_at=published_at(minutes), images=[], features=f, simhash=f"{simhash(toks):016x}", has_full_text=True, content_hash=sha256_hex(title + body),
            ))  # fmt: skip


@pytest.fixture
def corpus(ctx: Container, project: int) -> int:
    _load_corpus(ctx, project)
    return project


@pytest.fixture
def load_corpus():
    return _load_corpus


@pytest.fixture
def pipeline(ctx: Container):
    """Кластеризация → оценка → воронка. Возвращает созданный FunnelRun (id)."""

    def run(project_id: int, actor: Actor | None = None) -> int:
        actor = actor or Actor.user(1, "admin")
        ctx.events.process_new(project_id)
        with ctx.db.session() as s:
            ctx.scoring.score_recent(s, project_id)
        with ctx.db.session() as s:
            return ctx.funnel.run(s, project_id, actor).id

    return run


def add_article(ctx: Container, project_id: int, source_key: str, title: str, body: str, *, minutes: int, cite: list[str] | None = None, flags: dict | None = None, url: str | None = None, lang_hint: str | None = None) -> int:
    """Вставляет произвольную статью с признаками (для сценариев, которых нет в базовом корпусе)."""
    from smi_agent.core.text import sha256_hex, simhash, tokenize
    from smi_agent.ingestion.features import build_alias_index, compute_features

    with ctx.db.session() as s:
        srcs = list(s.scalars(select(Source).where(Source.project_id == project_id)))
        src = next(x for x in srcs if x.key == source_key)
        f = compute_features(title, "", body, know=ctx.know, alias_index=build_alias_index(srcs)).as_dict()
        if cite:
            f["attribution"] = {"keys": cite, "names": [], "marker_count": 1}
        if flags:
            f["flags"].update(flags)
        toks = tokenize(f"{title} {body[:1000]}", lang=f["lang"])
        url = url or f"https://news.example/{sha256_hex(title + source_key)[:12]}"
        a = Article(project_id=project_id, source_id=src.id, url=url, canonical_url=canonical_url(url), url_hash=url_hash(url), title=title, summary="", body=body, lang=f["lang"], published_at=published_at(minutes), fetched_at=published_at(minutes), images=[], features=f, simhash=f"{simhash(toks):016x}", has_full_text=True, content_hash=sha256_hex(title + body))
        s.add(a)
        s.flush()
        return a.id


RATE_CORE = {
    "headline": "Базовая ставка в Казахстане не изменилась: регулятор оставил 16,5%",
    "lead": "Регулятор решил не трогать ключевой ориентир для кредитов — по данным Kursiv.media, он остаётся на уровне 16,5% годовых.",
    "points": [
        "Инфляция в августе замедлилась до 12,3% — такие данные приводит Бюро национальной статистики.",
        "Председатель Нацбанка Тимур Сулейменов предупредил, что риски остаются высокими.",
        "Очередное решение по ставке ожидается осенью, в ноябре.",
    ],
    "context": "Аналитики заранее ждали именно такого исхода.",
    "why_it_matters": "Базовая ставка — ориентир для стоимости кредитов и депозитов в банках.",
    "used_fact_ids": [1, 2, 3],
}
RATE_PLATFORMS = {
    "telegram": {"title": "Ставка 16,5%", "body": "<b>Нацбанк оставил ставку прежней — 16,5%</b>\n\nКлючевой ориентир для кредитов не изменился. Инфляция замедлилась до 12,3%, но риски остаются высокими, предупредил председатель Нацбанка Тимур Сулейменов.\n\nСледующее решение — в ноябре (по данным Kursiv.media).\n\nИсточники: NUR.KZ, Kursiv.media, Tengrinews", "hashtags": ["#ставка", "#Казахстан"]},
    "instagram": {"title": "Ставка 16,5%", "body": "Ставка осталась 16,5% 💬\n\nРегулятор не стал менять ориентир для кредитов (по данным Kursiv.media). Инфляция в августе — 12,3%.\n\nИсточники: NUR.KZ, Kursiv.media", "hashtags": ["#ставка", "#финансы", "#Казахстан"], "slides": ["Ставка осталась 16,5%", "Инфляция в августе: 12,3%", "Риски остаются высокими", "Следующее решение — в ноябре"], "reels": {"hook": "Ставка не изменилась", "beats": ["16,5% — без изменений", "Инфляция замедлилась до 12,3%"], "cta": "Источники — в описании"}},
    "facebook": {"title": "Ставка 16,5%", "body": "Нацбанк Казахстана сохранил базовую ставку — 16,5% годовых.\n\nПочему важно: от неё зависят ставки по кредитам и депозитам. Инфляция в августе составила 12,3%, а глава регулятора Тимур Сулейменов отметил сохраняющиеся риски — по данным Kursiv.media.\n\nИсточники: NUR.KZ, Kursiv.media", "hashtags": ["#Казахстан"]},
}


@pytest.fixture
def fake_llm():
    from smi_agent.llm.testing import ScriptedLlm

    return ScriptedLlm(core=RATE_CORE, platforms=RATE_PLATFORMS)


@pytest.fixture
def llm_ctx(settings, clock, fake_llm):
    c = Container(settings, clock=clock, llm=fake_llm)
    c.db.create_all()
    yield c
    c.close()


@pytest.fixture
def published(llm_ctx, corpus, pipeline, admin):
    """Материал, опубликованный в песочницах трёх платформ. Возвращает (ctx, project_id, content_pk, [publication ids])."""
    from smi_agent.db.models import Proposal

    ctx = llm_ctx
    pipeline(corpus)
    with ctx.db.read() as s:
        pid = s.scalars(select(Proposal).where(Proposal.slot == 1)).first().id
    content_pk = ctx.content.create(corpus, pid, {}, admin)
    with ctx.db.session() as s:
        for p in ("telegram", "instagram", "facebook"):
            ctx.accounts.connect(s, admin, corpus, platform=p, sandbox=True, mode="manual", display_name=f"{p} demo")
    with ctx.db.session() as s:
        pubs = ctx.publishing.create_for_content(s, corpus, content_pk, admin)
        ids = [p.id for p in pubs]
        for p in pubs:
            ctx.publishing.approve(s, corpus, p.id, admin)
    assert all(r["state"] == "published" for r in ctx.publishing.run_due())
    return ctx, corpus, content_pk, ids
