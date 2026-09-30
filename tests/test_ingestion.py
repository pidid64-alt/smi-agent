from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from smi_agent.core.errors import Conflict, ValidationFailed
from smi_agent.db.models import Article, Project, Source
from smi_agent.ingestion.feeds import parse_items, parse_sitemap
from smi_agent.ingestion.normalize import canonical_url, clean_title, html_to_text, parse_datetime, url_hash
from smi_agent.monitoring.http import SafeHttp
from smi_agent.security.rbac import Actor

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Тест</title>
<item><title>Нацбанк сохранил базовую ставку на уровне 16,5% | NUR.KZ</title><link>https://www.nur.kz/economy/1/?utm_source=tg</link>
<description><![CDATA[<p>Национальный банк Казахстана сохранил базовую ставку на уровне 16,5% годовых. Решение принято на заседании Комитета.</p><p>Читайте также: другая новость</p><img src="https://pixel.nur.kz/1.gif" width="1" height="1"><p>Инфляция в августе составила 12,3%.</p>]]></description>
<pubDate>Wed, 30 Sep 2026 14:05:00 +0500</pubDate><category>Экономика</category>
<media:content url="https://cdn.nur.kz/a.jpg" medium="image"/></item>
<item><title>Старая новость</title><link>https://www.nur.kz/old/2</link><description>Старый текст</description><pubDate>Mon, 01 Jan 2024 10:00:00 +0500</pubDate></item>
<item><title>Будущая новость</title><link>https://www.nur.kz/future/3</link><description>Текст</description><pubDate>Fri, 01 Jan 2027 10:00:00 +0500</pubDate></item>
</channel></rss>"""

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://www.reuters.com/s/1.xml</loc><lastmod>2026-09-30T10:00:00Z</lastmod></sitemap><sitemap><loc>https://www.reuters.com/s/0.xml</loc><lastmod>2026-09-29T10:00:00Z</lastmod></sitemap></sitemapindex>"""
NEWS_SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://www.reuters.com/world/x-1</loc><news:news><news:publication><news:name>Reuters</news:name></news:publication><news:publication_date>2026-09-30T09:00:00Z</news:publication_date><news:title>Kazakhstan central bank holds rate</news:title></news:news></url></urlset>"""
XXE = """<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://a.kz/&x;</loc><news:news><news:title>&x;</news:title><news:publication_date>2026-09-30T09:00:00Z</news:publication_date></news:news></url></urlset>"""


def test_url_canonicalization_strips_tracking_and_unifies():
    assert canonical_url("HTTP://www.NUR.kz/news/123/?utm_source=tg&b=2&a=1#frag") == "https://nur.kz/news/123?a=1&b=2"
    assert url_hash("https://nur.kz/n/1?a=1&b=2") == url_hash("http://www.nur.kz/n/1/?b=2&a=1&utm_medium=x")


def test_html_to_text_drops_junk_and_trackers():
    text, imgs = html_to_text("<div><p>Факт один.</p><p>Читайте также: X</p><img src='https://pixel.nur.kz/1.gif'><img src='https://cdn.nur.kz/a.jpg'><div class='share'>SHARE</div><script>alert(1)</script></div>")
    assert text == "Факт один." and imgs == ["https://cdn.nur.kz/a.jpg"]


def test_title_cleanup_and_dates():
    assert clean_title("Ставка осталась прежней | NUR.KZ", ["NUR.KZ"]) == "Ставка осталась прежней"
    dt, flags = parse_datetime("Wed, 30 Sep 2026 14:05:00 +0500")
    assert dt.hour == 9 and not flags  # +05:00 → UTC
    dt, flags = parse_datetime("2026-09-30T14:05:00", default_tz="Asia/Almaty")
    assert dt.hour == 9 and "date_naive_tz_assumed" in flags
    from datetime import UTC, datetime

    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    dt, flags = parse_datetime("Fri, 01 Jan 2027 10:00:00 +0500", now=now)
    assert dt == now and "date_future" in flags


def test_sitemap_parsing_and_xxe_safety():
    items, kids = parse_sitemap(SITEMAP_INDEX.encode())
    assert not items and kids[0][0].endswith("/1.xml")  # самый свежий потомок первым
    items, _ = parse_sitemap(NEWS_SITEMAP.encode())
    assert items[0].title == "Kazakhstan central bank holds rate"
    items, _ = parse_sitemap(XXE.encode())
    assert all("root:" not in (i.title + i.url) for i in items)  # внешние сущности не раскрываются


def _transport(feed: str, *, etag: str = 'W/"1"', status: int = 200, calls: list | None = None):
    def handler(req: httpx.Request):
        if calls is not None:
            calls.append(dict(req.headers))
        if req.headers.get("if-none-match") == etag:
            return httpx.Response(304)
        return httpx.Response(status, content=feed.encode(), headers={"content-type": "application/rss+xml", "etag": etag})

    return httpx.MockTransport(handler)


def _swap_http(ctx, transport):
    ctx.http.close()
    ctx.http = SafeHttp(ctx.settings, transport=transport, resolver=lambda h, p: ["93.184.216.34"])


def test_poll_stores_articles_with_features_and_skips_old_and_future(ctx, project):
    _swap_http(ctx, _transport(RSS))
    with ctx.db.read() as s:
        nur = s.scalar(select(Source).where(Source.key == "nur_kz"))
        sid = nur.id
    res = ctx.ingest.poll_source(project, sid, cluster=False)
    assert res.ok and res.fetched == 3 and res.old_skipped == 1 and res.new == 2
    with ctx.db.read() as s:
        arts = list(s.scalars(select(Article).order_by(Article.id)))
        a = arts[0]
        assert a.title == "Нацбанк сохранил базовую ставку на уровне 16,5%"  # суффикс «| NUR.KZ» убран
        assert "Читайте также" not in a.body and a.canonical_url == "https://nur.kz/economy/1"
        assert a.published_at.hour == 9
        assert any(n["v"] == 16.5 for n in a.features["numbers"])
        assert [i["url"] for i in a.images] == ["https://cdn.nur.kz/a.jpg"]
        future = arts[1]
        assert future.published_at <= ctx.clock.now()  # будущая дата обрезана до «сейчас»
        assert s.get(Source, sid).last_success_at is not None


def test_repeated_poll_does_not_duplicate_and_uses_conditional_get(ctx, project):
    calls: list[dict] = []
    _swap_http(ctx, _transport(RSS, calls=calls))
    with ctx.db.read() as s:
        sid = s.scalar(select(Source.id).where(Source.key == "nur_kz"))
    ctx.ingest.poll_source(project, sid, cluster=False)
    ctx.clock.advance(minutes=20)
    res = ctx.ingest.poll_source(project, sid, cluster=False)
    assert res.ok and res.new == 0 and "304" in res.status
    assert calls[-1].get("if-none-match") == 'W/"1"'
    with ctx.db.read() as s:
        assert s.query(Article).count() == 2


def test_failure_backoff_and_health(ctx, project):
    _swap_http(ctx, _transport("", status=503))
    with ctx.db.read() as s:
        sid = s.scalar(select(Source.id).where(Source.key == "nur_kz"))
    for _ in range(3):
        res = ctx.ingest.poll_source(project, sid, cluster=False)
        assert not res.ok
        ctx.clock.advance(hours=8)
    with ctx.db.read() as s:
        src = s.get(Source, sid)
        assert src.consecutive_errors == 3 and src.state["next_attempt_at"]
        health = {h["key"]: h for h in ctx.ingest.source_health(s, project)}
        assert health["nur_kz"]["state"] == "failing"
        # несконфигурированный/не опрошенный источник помечается отдельно, отключённый — тоже
        assert health["ap_news"]["state"] == "disabled"


def test_due_sources_respect_interval_and_backoff(ctx, project):
    with ctx.db.read() as s:
        due = {x.key for x in ctx.ingest.due_sources(s, project)}
        assert "nur_kz" in due and "ap_news" not in due  # AP отключён (платный API)


def test_source_crud_rules(ctx, project, admin):
    with ctx.db.session() as s:
        nur = s.scalar(select(Source).where(Source.key == "nur_kz"))
        with pytest.raises(Conflict):
            ctx.ingest.delete_source(s, project, admin, nur.id)  # NUR.KZ обязателен
        with pytest.raises(Conflict):
            ctx.ingest.upsert_source(s, project, admin, {"enabled": False}, source_id=nur.id)  # отключение — только с подтверждением
        ctx.ingest.upsert_source(s, project, admin, {"enabled": False, "confirm_disable_mandatory": True}, source_id=nur.id)
        assert nur.enabled is False
        new = ctx.ingest.upsert_source(s, project, admin, {"key": "my_feed", "name": "Мой источник", "url": "https://example.com/rss", "kind": "rss"})
        assert new.config == {} and new.enabled
        with pytest.raises(ValidationFailed):
            ctx.ingest.upsert_source(s, project, admin, {"key": "bad", "url": "http://169.254.169.254/x", "kind": "rss"})  # SSRF отклонён при добавлении
        with pytest.raises(ValidationFailed):
            ctx.ingest.upsert_source(s, project, admin, {"key": "Bad Key!", "url": "https://example.com/rss"})
        ctx.ingest.delete_source(s, project, admin, new.id)  # без материалов — удаляется


def test_sources_are_config_only_no_code_needed(ctx, project, tmpdir_path):
    """Добавление источника через YAML-файл проекта (config/sources.yaml) не требует правки кода."""
    f = tmpdir_path / "sources.yaml"
    f.write_text("sources:\n  - {key: extra_kz, name: Extra KZ, kind: rss, url: 'https://extra.kz/rss', country: KZ, tier: quality}\n", encoding="utf-8")
    with ctx.db.session() as s:
        assert ctx.ingest.seed_sources(s, project, Actor.system(), extra_yaml=f) == 1
        assert s.scalar(select(Source.id).where(Source.key == "extra_kz"))


def test_paywalled_source_keeps_only_teaser(ctx, project):
    long_body = "<p>" + ("Платный текст. " * 200) + "</p>"
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>Markets rally</title><link>https://www.ft.com/content/1</link><description><![CDATA[{long_body}]]></description><pubDate>Wed, 30 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
    _swap_http(ctx, _transport(feed))
    with ctx.db.read() as s:
        sid = s.scalar(select(Source.id).where(Source.key == "ft_home"))
    ctx.ingest.poll_source(project, sid, cluster=False)
    with ctx.db.read() as s:
        a = s.scalar(select(Article))
        assert len(a.body) <= 500 and not a.has_full_text
