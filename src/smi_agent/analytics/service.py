"""Аналитика (ТЗ §40–49): графики за периоды, гео-доля, закономерности, отчёты, стратегия, дашборд из шести разделов."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import PubState
from ..core.errors import ValidationFailed
from ..db.models import (AuditLog, AudienceSnapshot, Content, Event, ForecastRecord, FunnelItem, FunnelRun, MetricSnapshot, PlatformAccount, Project, Proposal, Publication, Report)
from ..publishing.scheduling import tz_of
from ..settings_model import load_project_settings
from .metrics import METRIC_FIELDS, primary_value

PERIODS = {"today": ("hour",), "7d": ("day",), "30d": ("day",), "3m": ("week",), "6m": ("week",), "year": ("month",)}
METRICS = {"posts", "views", "reach", "engagement", "kz_share", "events", "trend_avg", "followers", "selected"}


def resolve_period(period: str, start: datetime | None, end: datetime | None, now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime, str]:
    if period == "custom":
        if start is None or end is None or end <= start:
            raise ValidationFailed("Для произвольного периода укажите корректные даты начала и конца")
        span = end - start
        if span > timedelta(days=800):
            raise ValidationFailed("Период не должен превышать ~2 лет")
        return start, end, "hour" if span <= timedelta(days=2) else "day" if span <= timedelta(days=62) else "week" if span <= timedelta(days=210) else "month"
    if period not in PERIODS:
        raise ValidationFailed("Период: today | 7d | 30d | 3m | 6m | year | custom")
    local_now = now.astimezone(tz)
    if period == "today":
        return local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC), now, "hour"
    days = {"7d": 7, "30d": 30, "3m": 91, "6m": 183, "year": 365}[period]
    return now - timedelta(days=days), now, PERIODS[period][0]


def bucket_start(dt: datetime, bucket: str, tz: ZoneInfo) -> datetime:
    d = dt.astimezone(tz)
    if bucket == "hour":
        return d.replace(minute=0, second=0, microsecond=0)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        return d - timedelta(days=d.weekday())
    if bucket == "month":
        return d.replace(day=1)
    return d


def next_bucket(b: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return b + timedelta(hours=1)
    if bucket == "day":
        return b + timedelta(days=1)
    if bucket == "week":
        return b + timedelta(days=7)
    return (b.replace(day=28) + timedelta(days=4)).replace(day=1)


def label_of(b: datetime, bucket: str) -> str:
    return b.strftime({"hour": "%d.%m %H:00", "day": "%d.%m", "week": "%d.%m", "month": "%m.%Y"}[bucket])


class AnalyticsService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def _cfg(self, s: Session, project_id: int):
        return load_project_settings(s.get(Project, project_id).settings)

    # ------------------------------------------------------------------- графики
    def series(self, s: Session, project_id: int, metric: str, period: str, *, start: datetime | None = None, end: datetime | None = None, platform: str | None = None) -> dict[str, Any]:
        if metric not in METRICS:
            raise ValidationFailed("Метрика: " + ", ".join(sorted(METRICS)))
        cfg = self._cfg(s, project_id)
        tz = tz_of(cfg)
        now = self.ctx.clock.now()
        t0, t1, bucket = resolve_period(period, start, end, now, tz)
        edges: list[datetime] = []
        b = bucket_start(t0, bucket, tz)
        while b <= t1.astimezone(tz) and len(edges) < 400:
            edges.append(b)
            b = next_bucket(b, bucket)
        idx = {e: i for i, e in enumerate(edges)}
        labels = [label_of(e, bucket) for e in edges]
        series: dict[str, list[float | None]] = defaultdict(lambda: [0.0] * len(edges))
        unavailable: set[str] = set()

        def put(name: str, dt: datetime, val: float) -> None:
            i = idx.get(bucket_start(dt, bucket, tz))
            if i is not None:
                series[name][i] += val

        if metric in ("posts", "views", "reach", "engagement", "kz_share"):
            pubs = list(s.scalars(select(Publication).where(Publication.project_id == project_id, Publication.state == PubState.PUBLISHED.value, Publication.published_at >= t0, Publication.published_at <= t1)))
            if platform:
                pubs = [p for p in pubs if p.platform == platform]
            if metric == "kz_share":
                kz, tot = [0.0] * len(edges), [0.0] * len(edges)
                for p in pubs:
                    c = s.get(Content, p.content_pk)
                    i = idx.get(bucket_start(p.published_at, bucket, tz))
                    if i is not None:
                        tot[i] += 1
                        kz[i] += 1 if c.geo in ("kz", "ca") else 0
                series["Доля КЗ"] = [round(k / t, 3) if t else None for k, t in zip(kz, tot, strict=True)]
                series["Цель"] = [cfg.geo.kz_target] * len(edges)
            else:
                for p in pubs:
                    if metric == "posts":
                        put(p.platform, p.published_at, 1)
                        continue
                    snap = s.scalars(select(MetricSnapshot).where(MetricSnapshot.publication_id == p.id).order_by(MetricSnapshot.age_hours.desc(), MetricSnapshot.id.desc()).limit(1)).first()
                    if snap is None:
                        unavailable.add(p.platform)
                        continue
                    if metric == "engagement":
                        val = (snap.likes or 0) + (snap.comments or 0) + (snap.shares or 0) + (snap.saves or 0)
                        if not any(getattr(snap, k) is not None for k in ("likes", "comments", "shares", "saves")):
                            unavailable.add(p.platform)
                            continue
                    else:
                        val = getattr(snap, metric)
                        if val is None:
                            unavailable.add(p.platform)
                            continue
                    put(p.platform, p.published_at, float(val))
        elif metric in ("events", "trend_avg"):
            evs = list(s.scalars(select(Event).where(Event.project_id == project_id, Event.first_seen_at >= t0, Event.first_seen_at <= t1, Event.merged_into_id.is_(None))))
            sums, cnt = [0.0] * len(edges), [0] * len(edges)
            for e in evs:
                i = idx.get(bucket_start(e.first_seen_at, bucket, tz))
                if i is not None:
                    sums[i] += e.trend_score
                    cnt[i] += 1
            series["События" if metric == "events" else "Средний Trend Score"] = [float(c) if metric == "events" else (round(sm / c, 1) if c else None) for sm, c in zip(sums, cnt, strict=True)]
        elif metric == "selected":
            for p in s.scalars(select(Proposal).where(Proposal.project_id == project_id, Proposal.resolved_at >= t0, Proposal.resolved_at <= t1)):
                put({"selected": "Выбрано", "executed": "Выбрано", "rejected": "Отклонено", "replaced": "Заменено"}.get(p.status, "Прочее"), p.resolved_at, 1)
        elif metric == "followers":
            accs = list(s.scalars(select(PlatformAccount).where(PlatformAccount.project_id == project_id)))
            for a in accs:
                if platform and a.platform != platform:
                    continue
                last: int | None = None
                vals: list[int | None] = [None] * len(edges)
                for snap in s.scalars(select(AudienceSnapshot).where(AudienceSnapshot.account_id == a.id, AudienceSnapshot.ts <= t1).order_by(AudienceSnapshot.ts)):
                    i = idx.get(bucket_start(snap.ts, bucket, tz))
                    if i is not None:
                        vals[i] = snap.followers
                out: list[float | None] = []
                for v in vals:
                    last = v if v is not None else last
                    out.append(float(last) if last is not None else None)
                if any(v is not None for v in out):
                    series[f"{a.platform}: {a.display_name}"] = out
        data = [{"name": k, "points": [round(x, 3) if isinstance(x, float) else x for x in v]} for k, v in series.items()]
        return {"metric": metric, "period": period, "bucket": bucket, "start": t0.isoformat(), "end": t1.isoformat(), "labels": labels, "series": data, "unavailable_platforms": sorted(unavailable), "timezone": cfg.scheduling.timezone}

    # ----------------------------------------------------------------- гео-доля (§4, §10)
    def geo_ratio(self, s: Session, project_id: int, start: datetime, end: datetime) -> dict[str, Any]:
        cfg = self._cfg(s, project_id)
        pubs = list(s.scalars(select(Publication).where(Publication.project_id == project_id, Publication.state == PubState.PUBLISHED.value, Publication.published_at >= start, Publication.published_at <= end)))
        seen: set[int] = set()
        kz = 0
        total = 0
        for p in pubs:
            if p.content_pk in seen:
                continue
            seen.add(p.content_pk)
            c = s.get(Content, p.content_pk)
            total += 1
            kz += 1 if c.geo in ("kz", "ca") else 0
        props = list(s.scalars(select(Proposal).where(Proposal.project_id == project_id, Proposal.shown_at >= start, Proposal.shown_at <= end)))
        pkz = sum(1 for p in props if (p.card or {}).get("geo_bucket") == "KZ")
        return {
            "target": {"kz": cfg.geo.kz_target, "world": cfg.geo.world_target},
            "published": {"kz": kz, "world": total - kz, "kz_share": round(kz / total, 3) if total else None},
            "proposed": {"kz": pkz, "world": len(props) - pkz, "kz_share": round(pkz / len(props), 3) if props else None},
            "note": "Доля — мягкий ориентир, а не квота: слабые темы не добавлялись ради её выполнения.",
        }  # fmt: skip

    # --------------------------------------------------------------- закономерности (§42–43)
    def insights(self, s: Session, project_id: int, *, days: int = 90) -> dict[str, Any]:
        cfg = self._cfg(s, project_id)
        tz = tz_of(cfg)
        since = self.ctx.clock.now() - timedelta(days=days)
        rows = []
        for r in s.scalars(select(ForecastRecord).where(ForecastRecord.project_id == project_id, ForecastRecord.evaluated_at.is_not(None), ForecastRecord.created_at >= since)):
            c = s.get(Content, r.content_pk)
            pub = s.scalars(select(Publication).where(Publication.content_pk == c.id, Publication.state == PubState.PUBLISHED.value)).first()
            if pub is None or r.actual.get("log_ratio") is None:
                continue
            local = pub.published_at.astimezone(tz)
            has_numbers = any(i.get("kind") == "number" for i in (c.fact_base or []))
            rows.append({
                "y": float(r.actual["log_ratio"]),
                "dims": {"category": c.category, "geo": c.geo, "format": (c.draft or {}).get("format_code", "?"), "platform": pub.platform, "hour": f"{local.hour:02d}:00", "weekday": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][local.weekday()], "numbers": "с цифрами" if has_numbers else "без цифр", **{f"subtopic:{t}": t for t in (c.subtopics or [])[:2]}},
            })  # fmt: skip
        if len(rows) < 8:
            return {"n": len(rows), "findings": [], "note": f"Недостаточно данных для выводов: оценено публикаций {len(rows)} из 8 необходимых. Закономерности не подбираются «на глаз»."}
        overall = statistics.fmean(r["y"] for r in rows)
        groups: dict[tuple[str, str], list[float]] = defaultdict(list)
        for r in rows:
            for dim, key in r["dims"].items():
                groups[(dim.split(":")[0], key)].append(r["y"])
        sd = statistics.pstdev(r["y"] for r in rows) or 1.0
        findings = []
        for (dim, key), ys in groups.items():
            if len(ys) < 4:
                continue
            m = statistics.fmean(ys)
            diff = m - overall
            se = sd / (len(ys) ** 0.5)
            if abs(diff) >= 0.15 and abs(diff) > 1.5 * se:
                label = self.ctx.know.category_label(key) if dim == "category" else key
                findings.append({"dimension": dim, "key": key, "label": label, "n": len(ys), "effect": round(diff, 2), "ratio": round(2.718281828 ** diff, 2), "confidence": "высокая" if len(ys) >= 12 and abs(diff) > 3 * se else "средняя" if len(ys) >= 6 else "низкая",
                                 "text": f"{dim}: «{label}» — результат {'выше' if diff > 0 else 'ниже'} обычного в ~{abs(round(2.718281828 ** diff - 1, 2)) * 100:.0f}% (n={len(ys)})"})  # fmt: skip
        findings.sort(key=lambda f: -abs(f["effect"]))
        return {"n": len(rows), "overall_log_ratio": round(overall, 3), "findings": findings[:10], "note": "Это наблюдения по вашим публикациям, а не доказанные причины: проверяйте на новых данных."}

    # ------------------------------------------------------------------- дашборд
    def dashboard(self, s: Session, project_id: int) -> dict[str, Any]:
        now = self.ctx.clock.now()
        cfg = self._cfg(s, project_id)
        events = list(s.scalars(select(Event).where(Event.project_id == project_id, Event.merged_into_id.is_(None), Event.last_update_at >= now - timedelta(hours=cfg.funnel.max_event_age_hours)).order_by(Event.trend_score.desc()).limit(15)))
        pool = [{"id": e.id, "title": e.title, "category": e.category, "category_label": self.ctx.know.category_label(e.category), "geo": e.geo, "trend_score": e.trend_score, "phase": e.phase, "n_independent": e.n_independent, "n_articles": e.n_articles, "velocity": e.velocity, "verification": e.verification_status, "stage": e.stage, "first_published_at": e.first_published_at.isoformat(), "explain": (e.components or {}).get("explain", {}).get("velocity", "")} for e in events]
        run = s.scalars(select(FunnelRun).where(FunnelRun.project_id == project_id).order_by(FunnelRun.id.desc()).limit(1)).first()
        props = [p for p in self.ctx.interaction.current(s, project_id)]
        pubs = list(s.scalars(select(Publication).where(Publication.project_id == project_id).order_by(Publication.id.desc()).limit(40)))
        contents = self.ctx.content.list(s, project_id, limit=20)
        week = self.ctx.clock.now() - timedelta(days=7)
        return {
            "agenda": {"events": pool, "funnel": {"counts": run.counts, "geo_ratio": run.geo_ratio, "notes": run.notes, "finished_at": run.finished_at.isoformat() if run.finished_at else None} if run else None, "sources": self.ctx.ingest.source_health(s, project_id)},
            "proposals": [{"id": p.id, "slot": p.slot, "status": p.status, "card": p.card, "expanded": bool(p.expanded)} for p in props],
            "content": [{"id": c.id, "content_id": c.content_id, "title": c.title, "status": c.status, "generator": c.generator, "category": c.category, "origin": c.origin, "created_at": c.created_at.isoformat()} for c in contents],
            "analytics": {"geo": self.geo_ratio(s, project_id, week, now), "forecast": self.ctx.metrics.forecast_accuracy(s, project_id), "posts_7d": self.series(s, project_id, "posts", "7d")},
            "profile": self.ctx.profile.snapshot(s, project_id),
            "publications": [self.ctx.publishing.to_dict(s, p) for p in pubs],
            "mode": cfg.mode.value,
            "kill_switches": [{"id": k.id, "scope": k.scope_type, "value": k.scope_value, "reason": k.reason, "engaged_at": k.engaged_at.isoformat(), "engaged_by": k.engaged_by} for k in self.ctx.killswitch.active(s, project_id)],
        }  # fmt: skip

    # ------------------------------------------------------------------- отчёты
    def build_report(self, s: Session, project_id: int, kind: str, *, end: datetime | None = None) -> Report:
        if kind not in ("weekly", "monthly", "strategy"):
            raise ValidationFailed("Тип отчёта: weekly | monthly | strategy")
        end = end or self.ctx.clock.now()
        days = {"weekly": 7, "monthly": 30, "strategy": 90}[kind]
        start = end - timedelta(days=days)
        cfg = self._cfg(s, project_id)
        pubs = list(s.scalars(select(Publication).where(Publication.project_id == project_id, Publication.state == PubState.PUBLISHED.value, Publication.published_at >= start, Publication.published_at <= end)))
        by_platform = Counter(p.platform for p in pubs)
        by_cat: Counter = Counter()
        posts = []
        for p in pubs:
            c = s.get(Content, p.content_pk)
            by_cat[c.category] += 1
            snap = s.scalars(select(MetricSnapshot).where(MetricSnapshot.publication_id == p.id).order_by(MetricSnapshot.age_hours.desc()).limit(1)).first()
            v = primary_value(snap) if snap else None
            posts.append({"publication_id": p.id, "content_id": c.content_id, "title": c.title, "platform": p.platform, "category": c.category, "value": v, "url": p.external_url})
        ranked = sorted((x for x in posts if x["value"]), key=lambda x: -x["value"])
        runs = list(s.scalars(select(FunnelRun).where(FunnelRun.project_id == project_id, FunnelRun.started_at >= start, FunnelRun.started_at <= end)))
        reasons: Counter = Counter()
        for it in s.scalars(select(FunnelItem).join(FunnelRun, FunnelRun.id == FunnelItem.run_id).where(FunnelRun.project_id == project_id, FunnelRun.started_at >= start, FunnelItem.decision == "dropped")):
            for r in it.reasons:
                reasons[r.split(":")[0][:60]] += 1
        props = list(s.scalars(select(Proposal).where(Proposal.project_id == project_id, Proposal.shown_at >= start, Proposal.shown_at <= end)))
        pc = Counter(p.status for p in props)
        ap_blocked = [a.details for a in s.scalars(select(AuditLog).where(AuditLog.project_id == project_id, AuditLog.action == "autopilot.decision", AuditLog.ts >= start))]
        geo = self.geo_ratio(s, project_id, start, end)
        acc = self.ctx.metrics.forecast_accuracy(s, project_id)
        ins = self.insights(s, project_id, days=max(days, 90))
        recs = self._recommendations(cfg, geo, pc, ins, acc, runs)
        payload = {
            "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days}, "publications": {"total": len(pubs), "by_platform": dict(by_platform), "by_category": dict(by_cat)},
            "top": ranked[:3], "bottom": ranked[-3:] if len(ranked) > 3 else [], "geo": geo, "funnel": {"runs": len(runs), "avg_counts": {k: round(statistics.fmean([r.counts.get(k, 0) for r in runs]), 1) for k in ("pool", "s50", "s15", "s10", "s5")} if runs else {}, "top_drop_reasons": reasons.most_common(6)},
            "proposals": {"shown": len(props), **dict(pc)}, "autopilot": {"decisions": len(ap_blocked), "blocked_reasons_sample": [d.get("skipped") for d in ap_blocked[-3:]]},
            "forecast": acc, "insights": ins, "recommendations": recs,
        }  # fmt: skip
        md = self._markdown(kind, payload)
        rep = Report(project_id=project_id, kind=kind, period_start=start, period_end=end, payload=payload, markdown=md, created_at=self.ctx.clock.now())
        s.add(rep)
        s.flush()
        return rep

    def _recommendations(self, cfg, geo, pc, ins, acc, runs) -> list[str]:
        out: list[str] = []
        share = geo["published"]["kz_share"]
        if share is not None and abs(share - cfg.geo.kz_target) > 0.15 and geo["published"]["kz"] + geo["published"]["world"] >= 5:
            out.append(f"Фактическая доля КЗ в публикациях {share:.0%} при ориентире {cfg.geo.kz_target:.0%}: проверьте, не мешают ли фильтры/выбор ни мировым, ни местным темам.")
        shown = pc.get("rejected", 0) + pc.get("replaced", 0)
        if sum(pc.values()) >= 10 and shown / sum(pc.values()) > 0.5:
            out.append("Больше половины предложений отклоняется или заменяется — скорректируйте приоритетные категории и заблокированные темы в профиле.")
        for f in ins.get("findings", [])[:2]:
            out.append(f"Наблюдение: {f['text']}. Проверьте на следующих публикациях.")
        if acc.get("n", 0) >= 6 and acc.get("bias_log", 0) > 0.3:
            out.append("Система систематически переоценивает темы (прогноз выше факта) — прогнозы автоматически корректируются калибровкой.")
        if not runs:
            out.append("За период не было запусков воронки — проверьте расписание воркера и состояние источников.")
        return out or ["Существенных отклонений не обнаружено."]

    def _markdown(self, kind: str, p: dict[str, Any]) -> str:
        title = {"weekly": "Недельный отчёт", "monthly": "Месячный отчёт", "strategy": "Обзор стратегии"}[kind]
        g = p["geo"]
        lines = [f"# {title}", f"Период: {p['period']['start'][:10]} — {p['period']['end'][:10]}", "", "## Публикации", f"Всего: **{p['publications']['total']}**; по платформам: {p['publications']['by_platform'] or '—'}.", ""]
        lines += ["## Гео-баланс (КЗ / мир)", f"Ориентир: {g['target']['kz']:.0%} / {g['target']['world']:.0%}. Опубликовано: КЗ {g['published']['kz']}, мир {g['published']['world']}" + (f" (доля КЗ {g['published']['kz_share']:.0%})." if g["published"]["kz_share"] is not None else "."), ""]
        if p["top"]:
            lines += ["## Лучшие публикации"] + [f"- {x['title']} ({x['platform']}): {x['value']:.0f}" for x in p["top"]] + [""]
        lines += ["## Воронка", f"Запусков: {p['funnel']['runs']}. Средние размеры этапов: {p['funnel']['avg_counts'] or '—'}.", "Частые причины отсева: " + (", ".join(f"{r} ({n})" for r, n in p["funnel"]["top_drop_reasons"]) or "—"), ""]
        lines += ["## Предложения и выбор пользователя", f"Показано: {p['proposals']['shown']}; выбрано: {p['proposals'].get('selected', 0) + p['proposals'].get('executed', 0)}; отклонено: {p['proposals'].get('rejected', 0)}; заменено: {p['proposals'].get('replaced', 0)}.", ""]
        f = p["forecast"]
        lines += ["## Прогноз и факт", (f"Оценено публикаций: {f['n']}; средняя ошибка (log): {f.get('mae_log')}." if f.get("n") else f.get("note", "")), ""]
        lines += ["## Рекомендации"] + [f"- {r}" for r in p["recommendations"]]
        return "\n".join(lines)
