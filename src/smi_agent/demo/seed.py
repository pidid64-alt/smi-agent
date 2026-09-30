"""Демо-данные (SMI_DEMO_MODE=1): вымышленные новости, песочница вместо платформ, искусственная история и метрики. В production запрещено."""

from __future__ import annotations

import logging
import math
import os
import random
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ..core.text import sha256_hex, simhash, tokenize
from ..db.models import (
    Article,
    AudienceSnapshot,
    Content,
    ForecastRecord,
    MetricSnapshot,
    PlatformAccount,
    PlatformVersion,
    Project,
    Proposal,
    Publication,
    Source,
    UserAction,
)
from ..ingestion.features import build_alias_index, compute_features
from ..ingestion.normalize import canonical_url, url_hash
from ..security.rbac import Actor
from .corpus import DEMO_SOURCES, build_articles

log = logging.getLogger(__name__)
DEMO_PASSWORD_ENV = "SMI_DEMO_PASSWORD"


def demo_password() -> str:
    return os.environ.get(DEMO_PASSWORD_ENV, "smi-agent-showcase")


def seed_demo_if_empty(ctx: Any) -> bool:
    with ctx.db.read() as s:
        if s.scalar(select(Project.id).limit(1)):
            return False
    seed_demo(ctx)
    return True


def seed_demo(ctx: Any) -> dict[str, Any]:
    if ctx.settings.is_production:
        raise RuntimeError("Демо-данные запрещено создавать в production")
    now = ctx.clock.now()
    system = Actor.system()
    admin = Actor.user(0, "admin")
    with ctx.db.session() as s:
        proj = Project(slug="demo", name="Демо-редакция (вымышленные новости)", settings={"content": {"brand_name": "Демо-редакция"}})
        s.add(proj)
        s.flush()
        pid = proj.id
        ctx.ingest.seed_sources(s, pid, system)
        for src in s.scalars(select(Source).where(Source.project_id == pid)):
            src.enabled = False  # демо не ходит в сеть
            src.notes = (src.notes + " Демо-режим: опрос отключён.").strip()
        for key, name, country, tier, rel, group, official, wire in DEMO_SOURCES:
            s.add(Source(project_id=pid, key=key, name=name, kind="manual", url="", country=country, languages=["ru"] if country == "KZ" else ["ru", "en"], tier=tier, reliability=rel, independence_group=group, aliases=[name.split(" (")[0]], is_official=official, is_wire=wire, enabled=True, verified_url=False, config={"demo": True}, state={}, notes="Вымышленный источник для демонстрации.", timezone="UTC"))
        s.flush()
        pw = demo_password()
        users = {}
        for name, role, sup in (("demo", "admin", True), ("editor", "user", False), ("auditor", "auditor", False)):
            users[name] = ctx.auth.create_user(s, None, username=name, password=pw, display_name={"demo": "Демо-администратор", "editor": "Редактор", "auditor": "Аудитор"}[name], project_id=pid, role=role, superadmin=sup).id
        srcs = list(s.scalars(select(Source).where(Source.project_id == pid)))
        by_key = {x.key: x for x in srcs}
        alias = build_alias_index(srcs)
        n_art = 0
        for a in build_articles():
            src = by_key[a.source]
            f = compute_features(a.title, "", a.body, know=ctx.know, alias_index=alias).as_dict()
            if a.cite:
                f["attribution"] = {"keys": a.cite, "names": [], "marker_count": 1}
            toks = tokenize(f"{a.title} {a.body[:1000]}", lang=f["lang"])
            slug = sha256_hex(a.source + a.title + str(a.minutes_ago))[:14]
            url = f"https://demo.invalid/{a.source}/{slug}"
            ts = now - timedelta(minutes=a.minutes_ago)
            s.add(Article(project_id=pid, source_id=src.id, url=url, canonical_url=canonical_url(url), url_hash=url_hash(url), title=a.title, summary="", body=a.body, lang=f["lang"], published_at=ts, fetched_at=ts, images=[], features=f, simhash=f"{simhash(toks):016x}", has_full_text=True, content_hash=sha256_hex(a.title + a.body)))
            n_art += 1
        for plat in ("telegram", "instagram", "facebook"):
            ctx.accounts.connect(s, admin, pid, platform=plat, sandbox=True, mode="manual", display_name={"telegram": "Демо-канал Telegram", "instagram": "Демо-аккаунт Instagram", "facebook": "Демо-страница Facebook"}[plat])
    _seed_history(ctx, pid, now)
    ctx.events.process_new(pid)
    with ctx.db.session() as s:
        ctx.scoring.score_recent(s, pid)
    with ctx.db.session() as s:
        run = ctx.funnel.run(s, pid, system, trigger="demo")
        counts = run.counts
    log.warning("Демо-данные созданы: проект %s, статей %s, воронка %s. Логин: demo / пароль из %s (по умолчанию smi-agent-showcase)", pid, n_art, counts, DEMO_PASSWORD_ENV)
    return {"project_id": pid, "articles": n_art, "funnel": counts}


def _seed_history(ctx: Any, pid: int, now) -> None:
    """Искусственная история за 30 дней: прошлые публикации с метриками (source=demo), выборы пользователя и обучение профиля."""
    rnd = random.Random(7)
    cats = [("finance", "kz", ["banking"], 0.55), ("transport", "kz", ["tariffs_prices"], 0.45), ("economy", "kz", ["tariffs_prices"], 0.4), ("ai", "world", ["generative_ai"], 0.1), ("science", "world", [], -0.1), ("sport", "kz", [], -0.5), ("auto", "world", ["electric_vehicles"], 0.05), ("health", "kz", ["healthcare"], 0.3)]
    base = {"telegram": 1800, "instagram": 950, "facebook": 620}
    with ctx.db.session() as s:
        accs = {a.platform: a for a in s.scalars(select(PlatformAccount).where(PlatformAccount.project_id == pid))}
        for plat, acc in accs.items():
            for d in range(30, -1, -1):
                s.add(AudienceSnapshot(account_id=acc.id, ts=now - timedelta(days=d), followers=int({"telegram": 4200, "instagram": 2600, "facebook": 1500}[plat] * (1 + (30 - d) * 0.004) + rnd.randint(-8, 8)), extra={"demo": True}))
        n = 0
        for i in range(24):
            cat, geo, subs, effect = cats[i % len(cats)]
            published = now - timedelta(days=1 + i * 1.2, hours=rnd.randint(0, 9))
            c = Content(content_id=f"Content-{published.year}-{900000 + i:06d}", project_id=pid, title=f"[демо-история] материал №{i + 1}", category=cat, geo=geo, subtopics=subs, language="ru", angle="", status="published", generator="demo", fact_base=[], sources=[], draft={"format_code": "post_card"}, prediction={"interest": min(0.95, max(0.05, 0.5 + effect * 0.6 + rnd.uniform(-0.1, 0.1))), "value": 0.5}, sensitivity={}, origin="user", created_at=published, updated_at=published)
            s.add(c)
            s.flush()
            plat = ("telegram", "instagram", "facebook")[i % 3]
            v = PlatformVersion(content_pk=c.id, platform=plat, version=1, is_current=True, format="post", title=c.title, body="демо-история", language="ru", created_by="demo", created_at=published)
            s.add(v)
            s.flush()
            pub = Publication(project_id=pid, content_pk=c.id, platform_version_id=v.id, platform=plat, account_id=accs[plat].id, version=1, state="published", idempotency_key=sha256_hex(f"demo-history-{i}"), origin="user", approved_by="user:0", approved_at=published, scheduled_at=published, published_at=published, external_id=f"demo-{i}", external_url="", progress={"perf_recorded": True}, created_at=published, updated_at=published)
            s.add(pub)
            s.flush()
            lr = effect + rnd.uniform(-0.2, 0.2)
            views = int(base[plat] * math.exp(lr))
            for age in (24, 48):
                s.add(MetricSnapshot(publication_id=pub.id, captured_at=published + timedelta(hours=age), age_hours=age, views=int(views * (0.9 if age == 24 else 1.0)), reach=int(views * 0.8) if plat != "telegram" else None, likes=int(views * 0.04 * math.exp(lr / 2)), comments=int(views * 0.004), shares=int(views * 0.006), saves=int(views * 0.01) if plat == "instagram" else None, unavailable=[] if plat != "telegram" else ["reach", "saves"], source="demo", extra={"demo": True}))
            s.add(ForecastRecord(project_id=pid, content_pk=c.id, publication_id=pub.id, category=cat, prediction=dict(c.prediction), actual={"value": views, "log_ratio": round(lr, 3), "ratio": round(math.exp(lr), 2), "baseline": base[plat], "platform": plat}, error={"predicted_log_ratio": round((c.prediction["interest"] - 0.5) * 1.0, 3), "abs_error": round(abs((c.prediction["interest"] - 0.5) - lr), 3), "signed": round((c.prediction["interest"] - 0.5) - lr, 3)}, created_at=published, evaluated_at=published + timedelta(hours=48)))
            # прошлые предложения и выбор пользователя → профиль
            selected = effect > 0.2 or rnd.random() < 0.25
            s.add(Proposal(project_id=pid, run_id=None, slot=1 + i % 5, event_id=_dummy_event(s, pid, c, published), card={"title": c.title, "category": cat, "geo_bucket": "KZ" if geo == "kz" else "WORLD", "trend_score": 40}, prediction={}, status="selected" if selected else "rejected", shown_at=published - timedelta(hours=2), resolved_at=published - timedelta(hours=1), resolved_by="user:0"))
            ctx.learning.apply(s, pid, [("category", cat, 1.0 if selected else 0.0, 0.0 if selected else 1.0), ("geo", geo, 0.7 if selected else 0.0, 0.0 if selected else 0.7), *[("subtopic", t, 0.6 if selected else 0.0, 0.0 if selected else 0.6) for t in subs]], source="user_action", signal="select" if selected else "reject", ref_type="demo", ref_id=i)
            s.add(UserAction(project_id=pid, user_id=None, kind="select" if selected else "reject", payload={"demo": True}, raw_text=f"[демо] {'Беру' if selected else 'Неинтересна'} №{1 + i % 5}", created_at=published - timedelta(hours=1)))
            n += 1
        s.flush()


def _dummy_event(s, pid: int, c: Content, ts) -> int:
    from ..db.models import Event

    e = Event(project_id=pid, title=c.title, first_seen_at=ts, first_published_at=ts, last_update_at=ts, category=c.category, geo="kz" if c.geo == "kz" else "world", geo_bucket="KZ" if c.geo == "kz" else "WORLD", stage="published", n_articles=1, n_sources=1, n_independent=1)
    s.add(e)
    s.flush()
    c.event_id = e.id
    return e.id
