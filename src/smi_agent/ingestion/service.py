"""Мониторинг источников и сбор материалов (модули «Мониторинг» и «Сбор и нормализация», ТЗ §5–7, §61.1–2)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import AppError, Conflict, FetchError, NotFound, SSRFBlocked, ValidationFailed
from ..core.text import hamming, sha256_hex, simhash, tokenize
from ..db.models import Article, Project, Source
from ..knowledge import load_yaml_default
from ..security.rbac import Actor
from ..settings_model import load_project_settings
from .features import build_alias_index, compute_features
from .feeds import RawItem, extract_main_text, parse_items
from .normalize import canonical_url, clean_title, first_paragraphs, html_to_text, parse_datetime, url_hash

log = logging.getLogger(__name__)

SOURCE_KINDS = {"rss", "atom", "sitemap", "html_list", "json_feed", "manual"}
TIERS = {"primary", "quality", "specialized", "aggregator"}
_KEY_RX = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,62}$")
_SOURCE_FIELDS = {
    "name", "kind", "url", "site_url", "country", "languages", "tier", "category_hint", "reliability", "independence_group", "aliases",
    "is_official", "is_wire", "paywalled", "fulltext_policy", "media_policy", "enabled", "verified_url", "poll_interval_min", "timezone",
    "config", "notes",
}  # fmt: skip


@dataclass
class PollResult:
    source_key: str
    ok: bool
    status: str
    fetched: int = 0
    new: int = 0
    old_skipped: int = 0
    article_ids: list[int] = field(default_factory=list)


class IngestService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---------------------------------------------------------------- реестр источников
    def seed_sources(self, s: Session, project_id: int, actor: Actor, *, extra_yaml: str | Path | None = None) -> int:
        """Добавляет недостающие источники из defaults/sources.yaml и (опционально) config/sources.yaml. Существующие не трогает."""
        data = load_yaml_default("sources")
        entries: list[dict[str, Any]] = list(data.get("sources", []))
        if extra_yaml and Path(extra_yaml).exists():
            extra = yaml.safe_load(Path(extra_yaml).read_text(encoding="utf-8")) or {}
            by_key = {e["key"]: e for e in entries}
            for e in extra.get("sources", []):
                by_key[e["key"]] = {**by_key.get(e["key"], {}), **e}
            entries = list(by_key.values())
        existing = set(s.scalars(select(Source.key).where(Source.project_id == project_id)))
        added = 0
        for e in entries:
            if e["key"] in existing:
                continue
            self._create(s, project_id, e)
            added += 1
        if added:
            self.ctx.audit.log(s, actor, "source.seed", project_id=project_id, details={"added": added})
        return added

    def _create(self, s: Session, project_id: int, e: dict[str, Any]) -> Source:
        cfg = dict(e.get("config") or {})
        if e.get("mandatory"):
            cfg["mandatory"] = True
        src = Source(
            project_id=project_id, key=e["key"], name=e["name"], kind=e.get("kind", "rss"), url=e.get("url", ""), site_url=e.get("site_url", ""),
            country=e.get("country", "KZ"), languages=list(e.get("languages", ["ru"])), tier=e.get("tier", "quality"),
            category_hint=e.get("category_hint", ""), reliability=float(e.get("reliability", 0.7)),
            independence_group=e.get("independence_group", "") or e["key"], aliases=list(e.get("aliases", [])),
            is_official=bool(e.get("is_official", False)), is_wire=bool(e.get("is_wire", False)), paywalled=bool(e.get("paywalled", False)),
            fulltext_policy=e.get("fulltext_policy", "feed_only"), media_policy=e.get("media_policy", "none"),
            enabled=bool(e.get("enabled", True)), verified_url=bool(e.get("verified_url", False)),
            poll_interval_min=int(e.get("poll_interval_min", 15)), timezone=e.get("timezone", "Asia/Almaty"), config=cfg,
            state={}, notes=e.get("notes", ""),
        )  # fmt: skip
        s.add(src)
        s.flush()
        return src

    def validate_source(self, data: dict[str, Any], *, creating: bool) -> dict[str, Any]:
        d = {k: v for k, v in data.items() if k in _SOURCE_FIELDS or k in ("key", "mandatory")}
        if creating and not _KEY_RX.match(str(d.get("key", ""))):
            raise ValidationFailed("Ключ источника: латиница, цифры, _ и -, 2–63 символа")
        if "kind" in d and d["kind"] not in SOURCE_KINDS:
            raise ValidationFailed(f"Тип источника должен быть одним из: {', '.join(sorted(SOURCE_KINDS))}")
        if "tier" in d and d["tier"] not in TIERS:
            raise ValidationFailed("Недопустимый уровень источника")
        if "reliability" in d and not (0 <= float(d["reliability"]) <= 1):
            raise ValidationFailed("Надёжность — число от 0 до 1")
        if "poll_interval_min" in d and int(d["poll_interval_min"]) < 5:
            raise ValidationFailed("Интервал опроса — не чаще раза в 5 минут")
        kind = d.get("kind", "rss")
        url = d.get("url", "")
        if kind != "manual" and (creating or "url" in d):
            if not url:
                raise ValidationFailed("Укажите адрес ленты")
            try:
                self.ctx.http.guard.precheck(url)  # схема/порт/IP-литералы/запрещённые имена; DNS проверяется при каждом опросе
            except SSRFBlocked as e:
                raise ValidationFailed(f"Адрес отклонён политикой безопасности: {e.message}") from e
        if "config" in d and not isinstance(d["config"], dict):
            raise ValidationFailed("config должен быть объектом")
        return d

    def upsert_source(self, s: Session, project_id: int, actor: Actor, data: dict[str, Any], *, source_id: int | None = None) -> Source:
        if source_id is None:
            d = self.validate_source(data, creating=True)
            if s.scalar(select(Source.id).where(Source.project_id == project_id, Source.key == d["key"])):
                raise Conflict("Источник с таким ключом уже существует")
            d.setdefault("name", d["key"])
            src = self._create(s, project_id, d)
            self.ctx.audit.log(s, actor, "source.create", project_id=project_id, target_type="source", target_id=src.id, details={"key": src.key, "url": src.url})
            return src
        src = s.get(Source, source_id)
        if src is None or src.project_id != project_id:
            raise NotFound("Источник не найден")
        d = self.validate_source(data, creating=False)
        d.pop("key", None)
        mandatory = bool((src.config or {}).get("mandatory"))
        if mandatory and d.get("enabled") is False and not data.get("confirm_disable_mandatory"):
            raise Conflict("NUR.KZ — обязательный источник (ТЗ §6). Отключение требует явного подтверждения", code="mandatory_source")
        changed = {}
        for k, v in d.items():
            if k == "config":
                v = {**v, **({"mandatory": True} if mandatory else {})}
            if getattr(src, k) != v:
                changed[k] = v
                setattr(src, k, v)
        if "url" in changed:
            src.verified_url, src.state, src.consecutive_errors = False, {}, 0
        s.flush()
        self.ctx.audit.log(s, actor, "source.update", project_id=project_id, target_type="source", target_id=src.id, details={"key": src.key, "changed": list(changed)})
        return src

    def delete_source(self, s: Session, project_id: int, actor: Actor, source_id: int) -> None:
        src = s.get(Source, source_id)
        if src is None or src.project_id != project_id:
            raise NotFound("Источник не найден")
        if (src.config or {}).get("mandatory"):
            raise Conflict("Обязательный источник нельзя удалить; его можно отключить с подтверждением", code="mandatory_source")
        n = s.query(Article).filter(Article.source_id == src.id).count()
        if n:
            src.enabled = False  # история материалов сохраняется (аналитика, проверка первоисточников)
            self.ctx.audit.log(s, actor, "source.disable", project_id=project_id, target_type="source", target_id=src.id, details={"key": src.key, "reason": "has_articles"})
            return
        s.delete(src)
        self.ctx.audit.log(s, actor, "source.delete", project_id=project_id, target_type="source", target_id=source_id, details={"key": src.key})

    # --------------------------------------------------------------------- опрос
    def due_sources(self, s: Session, project_id: int) -> list[Source]:
        now = self.ctx.clock.now()
        out = []
        for src in s.scalars(select(Source).where(Source.project_id == project_id, Source.enabled.is_(True))):
            if src.kind == "manual" or not src.url:
                continue
            nxt = (src.state or {}).get("next_attempt_at")
            if nxt and datetime.fromisoformat(nxt) > now:
                continue
            if src.last_fetch_at and now - src.last_fetch_at < timedelta(minutes=src.poll_interval_min):
                continue
            out.append(src)
        return out

    def poll_due(self, project_id: int, *, cluster: bool = True) -> list[PollResult]:
        with self.ctx.db.read() as s:
            ids = [x.id for x in self.due_sources(s, project_id)]
        results = [self.poll_source(project_id, sid, cluster=False) for sid in ids]
        if cluster and any(r.new for r in results):
            self.ctx.events.process_new(project_id)
        return results

    def poll_source(self, project_id: int, source_id: int, *, cluster: bool = True) -> PollResult:
        ctx = self.ctx
        now = ctx.clock.now()
        with ctx.db.read() as s:
            src = s.get(Source, source_id)
            if src is None or src.project_id != project_id:
                raise NotFound("Источник не найден")
            proj = s.get(Project, project_id)
            cfg = load_project_settings(proj.settings).monitoring
            alias_index = build_alias_index(list(s.scalars(select(Source).where(Source.project_id == project_id))))
            snap = {c: getattr(src, c) for c in ("id", "key", "name", "kind", "url", "state", "config", "timezone", "country", "aliases", "paywalled", "fulltext_policy", "poll_interval_min", "consecutive_errors", "media_policy", "reliability", "is_official")}
        headers: dict[str, str] = {}
        st = snap["state"] or {}
        if st.get("etag"):
            headers["If-None-Match"] = st["etag"]
        if st.get("last_modified"):
            headers["If-Modified-Since"] = st["last_modified"]
        try:
            res = ctx.http.get(snap["url"], headers=headers, check_robots=(snap["kind"] == "html_list"))
            if res.status >= 400:
                raise FetchError(f"HTTP {res.status}")
            items: list[RawItem] = []
            new_state = dict(st)
            if res.not_modified:
                status = "ok: без изменений (304)"
            else:
                items, children = parse_items(snap["kind"], res.content, base_url=snap["url"], config=snap["config"])
                for loc, _mod in children[: int((snap["config"] or {}).get("max_children", 1))]:
                    sub = ctx.http.get(loc)
                    sub_items, _ = parse_items("sitemap", sub.content)
                    items.extend(sub_items)
                new_state["etag"] = res.headers.get("etag", "")
                new_state["last_modified"] = res.headers.get("last-modified", "")
                status = ""
        except (FetchError, SSRFBlocked, AppError, ValueError) as e:
            return self._record_failure(project_id, source_id, getattr(e, "message", str(e)), snap)
        except Exception as e:  # noqa: BLE001 — сбой разбора одной ленты не должен ронять мониторинг
            log.exception("poll %s failed", snap["key"])
            return self._record_failure(project_id, source_id, f"Ошибка разбора: {type(e).__name__}", snap)

        stored = self._store_items(project_id, snap, items, alias_index, cfg, now)
        with ctx.db.session() as s:
            src = s.get(Source, source_id)
            src.last_fetch_at = now
            src.last_success_at = now
            src.consecutive_errors = 0
            src.items_last_fetch = len(items)
            newest = max((a["published_at"] for a in stored["meta"]), default=None)
            if newest and (src.last_item_at is None or newest > src.last_item_at):
                src.last_item_at = newest
            src.state = {**new_state, "next_attempt_at": None}
            src.last_status = status or f"ok: {len(items)} в ленте, новых {stored['new']}"
        result = PollResult(snap["key"], True, status or f"ok: {len(items)} в ленте, новых {stored['new']}", len(items), stored["new"], stored["old"], stored["ids"])
        if cluster and stored["new"]:
            ctx.events.process_new(project_id)
        return result

    def _record_failure(self, project_id: int, source_id: int, message: str, snap: dict[str, Any]) -> PollResult:
        now = self.ctx.clock.now()
        with self.ctx.db.session() as s:
            src = s.get(Source, source_id)
            src.last_fetch_at = now
            src.consecutive_errors += 1
            delay_min = min(snap["poll_interval_min"] * (2 ** min(src.consecutive_errors, 6)), 360)
            src.state = {**(src.state or {}), "next_attempt_at": (now + timedelta(minutes=delay_min)).isoformat()}
            src.last_status = f"ошибка: {message}"[:300]
        log.warning("source %s: %s", snap["key"], message)
        return PollResult(snap["key"], False, f"ошибка: {message}")

    # -------------------------------------------------------------------- хранение
    def _store_items(self, project_id: int, snap: dict[str, Any], items: list[RawItem], alias_index: dict[str, str], cfg: Any, now: datetime) -> dict[str, Any]:
        ctx = self.ctx
        know = ctx.know
        max_age = timedelta(days=cfg.max_item_age_days)
        site_names = [snap["name"], *(snap["aliases"] or [])]
        prepared: list[dict[str, Any]] = []
        seen_hash: set[str] = set()
        old = 0
        for it in items[:300]:
            uh = url_hash(it.url)
            if uh in seen_hash:
                continue
            seen_hash.add(uh)
            pub, dflags = parse_datetime(it.published_raw, default_tz=snap["timezone"], now=now, struct_time=it.published_struct)
            if pub is None:
                pub = now
            if now - pub > max_age:
                old += 1
                continue
            prepared.append({"item": it, "hash": uh, "pub": pub, "dflags": dflags})
        if not prepared:
            return {"new": 0, "old": old, "ids": [], "meta": []}
        with ctx.db.read() as s:
            known = set(s.scalars(select(Article.url_hash).where(Article.project_id == project_id, Article.url_hash.in_([p["hash"] for p in prepared]))))
        fresh = [p for p in prepared if p["hash"] not in known]
        fulltext_budget = cfg.fulltext_per_poll if (snap["fulltext_policy"] == "fetch_allowed" and not snap["paywalled"]) else 0
        rows: list[Article] = []
        meta: list[dict[str, Any]] = []
        for p in fresh:
            it: RawItem = p["item"]
            body_text, imgs = html_to_text(it.body_html or it.summary_html)
            summary_text, imgs2 = html_to_text(it.summary_html)
            images = list(dict.fromkeys([*it.images, *imgs, *imgs2]))[:6]
            if fulltext_budget and len(body_text) < 700:
                try:
                    page = ctx.http.get(it.url, check_robots=True, max_bytes=2_000_000)
                    if page.status == 200:
                        full = extract_main_text(page.content)
                        if len(full) > len(body_text):
                            body_text = full
                    fulltext_budget -= 1
                except (FetchError, SSRFBlocked):
                    fulltext_budget -= 1
            if snap["paywalled"]:
                body_text = body_text[:500]  # платный доступ: только анонс
            if not summary_text or summary_text == body_text or len(summary_text) > 700:
                summary_text = first_paragraphs(body_text, 2, 500)
            title = clean_title(it.title, site_names)
            feats = compute_features(title, summary_text, body_text, know=know, alias_index=alias_index, section=it.section)
            f = feats.as_dict()
            toks = tokenize(f"{title} {summary_text} {body_text[:1000]}", lang=feats.lang)
            f["flags"]["date_flags"] = p["dflags"]
            art = Article(
                project_id=project_id, source_id=snap["id"], url=it.url[:1500], canonical_url=canonical_url(it.url)[:1500], url_hash=p["hash"],
                title=title[:600], summary=summary_text, body=body_text, lang=feats.lang, author=(it.author or "")[:200], section=(it.section or "")[:120],
                tags=it.tags[:10], published_at=p["pub"], fetched_at=now,
                images=[{"url": u, "license": snap["media_policy"]} for u in images], videos=[], related_links=[],
                content_hash=sha256_hex(f"{title}|{body_text[:1000]}"), simhash=f"{simhash(toks):016x}", features=f,
                has_full_text=len(body_text) >= 700 and not snap["paywalled"],
            )  # fmt: skip
            rows.append(art)
            meta.append({"published_at": p["pub"]})
        with ctx.db.session() as s:
            for a in rows:
                s.add(a)
            s.flush()
            ids = [a.id for a in rows]
        return {"new": len(rows), "old": old, "ids": ids, "meta": meta}

    # --------------------------------------------------------------------- здоровье
    def source_health(self, s: Session, project_id: int) -> list[dict[str, Any]]:
        now = self.ctx.clock.now()
        proj = s.get(Project, project_id)
        stale_h = load_project_settings(proj.settings).monitoring.source_stale_hours
        out = []
        for src in s.scalars(select(Source).where(Source.project_id == project_id).order_by(Source.name)):
            last_ok = src.last_success_at
            last_item = src.last_item_at
            if not src.enabled:
                state = "disabled"
            elif src.kind == "manual" or not src.url:
                state = "manual"
            elif src.consecutive_errors >= 3:
                state = "failing"
            elif last_ok is None:
                state = "never_polled"
            elif last_item is None or (now - last_item) > timedelta(hours=stale_h * (4 if src.tier == "primary" else 1)):
                state = "stale"
            else:
                state = "ok"
            out.append({
                "id": src.id, "key": src.key, "name": src.name, "country": src.country, "tier": src.tier, "enabled": src.enabled, "kind": src.kind,
                "url": src.url, "verified_url": src.verified_url, "state": state, "last_status": src.last_status,
                "last_success_at": last_ok.isoformat() if last_ok else None, "last_item_at": last_item.isoformat() if last_item else None,
                "consecutive_errors": src.consecutive_errors, "mandatory": bool((src.config or {}).get("mandatory")),
                "reliability": src.reliability, "independence_group": src.independence_group, "notes": src.notes, "paywalled": src.paywalled,
            })  # fmt: skip
        return out


__all__ = ["IngestService", "PollResult", "hamming"]
