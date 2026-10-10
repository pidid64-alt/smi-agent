"""Мониторинг публикаций и статистика (ТЗ §33, §40–41): снимки метрик по возрастам, честная фиксация «недоступно», обратная связь в обучение."""

from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import AccountStatus, PubState
from ..core.errors import NotFound, ValidationFailed
from ..db.models import (
    AudienceSnapshot,
    Content,
    ForecastRecord,
    MetricSnapshot,
    PlatformAccount,
    Publication,
)
from ..security.rbac import Actor

log = logging.getLogger(__name__)
MILESTONES_H = [1, 3, 6, 12, 24, 48, 168]
METRIC_FIELDS = ("views", "reach", "likes", "comments", "shares", "saves", "clicks", "follows")


def primary_value(m: MetricSnapshot | dict[str, Any]) -> float | None:
    """Главная метрика поста: просмотры → охват → взвешенные реакции. None — метрик нет."""
    g = (lambda k: m.get(k)) if isinstance(m, dict) else (lambda k: getattr(m, k))
    for k in ("views", "reach"):
        if g(k):
            return float(g(k))
    eng = (g("likes") or 0) + 2 * (g("comments") or 0) + 3 * (g("shares") or 0) + (g("saves") or 0)
    return float(eng) if eng else None


def engagement_rate(m: MetricSnapshot) -> float | None:
    base = m.reach or m.views
    if not base:
        return None
    return round(((m.likes or 0) + (m.comments or 0) + (m.shares or 0) + (m.saves or 0)) / base, 4)


class MetricsService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------ сбор
    def collect_due(self, project_id: int) -> list[dict[str, Any]]:
        ctx = self.ctx
        now = ctx.clock.now()
        with ctx.db.read() as s:
            rows = list(s.execute(select(Publication.id).where(Publication.project_id == project_id, Publication.state == PubState.PUBLISHED.value, Publication.published_at >= now - timedelta(days=8), Publication.external_id != "")))
            ids = [r[0] for r in rows]
        out = []
        for pid in ids:
            try:
                res = self.collect_one(pid)
                if res:
                    out.append(res)
            except Exception as e:  # noqa: BLE001 — сбор метрик одной публикации не должен ронять остальные
                log.warning("metrics collect failed pub=%s: %s", pid, type(e).__name__)
        return out

    def collect_one(self, pub_id: int) -> dict[str, Any] | None:
        ctx = self.ctx
        now = ctx.clock.now()
        with ctx.db.session() as s:
            pub = s.get(Publication, pub_id)
            acc = s.get(PlatformAccount, pub.account_id) if pub.account_id else None
            if acc is None or acc.status == AccountStatus.REVOKED.value or pub.published_at is None:
                return None
            age = (now - pub.published_at).total_seconds() / 3600
            last = s.scalars(select(MetricSnapshot).where(MetricSnapshot.publication_id == pub.id, MetricSnapshot.source == "api").order_by(MetricSnapshot.age_hours.desc()).limit(1)).first()
            due = [m for m in MILESTONES_H if m <= age + 0.01 and (last is None or m > last.age_hours + 0.01)]
            if not due:
                return None
            adapter = ctx.accounts.adapter_for(acc)
            token = ctx.accounts.token_for(s, Actor.ai("metrics", pub.project_id), acc, purpose=f"metrics:{pub.id}")
            ext, fmt, platform = pub.external_id, "post", pub.platform
        try:
            data = adapter.fetch_metrics(ext, token, fmt=fmt)
        finally:
            token = ""  # noqa: F841
        with ctx.db.session() as s:
            snap = MetricSnapshot(
                publication_id=pub_id, captured_at=now, age_hours=round(age, 2), unavailable=list(data.get("unavailable", [])), source="api",
                extra={k: v for k, v in data.items() if k not in METRIC_FIELDS and k != "unavailable"},
                **{k: (int(data[k]) if data.get(k) is not None else None) for k in METRIC_FIELDS},
            )  # fmt: skip
            s.add(snap)
        return {"publication_id": pub_id, "platform": platform, "age_hours": round(age, 1), "unavailable": data.get("unavailable", [])}

    def collect_audience(self, project_id: int) -> int:
        ctx = self.ctx
        n = 0
        with ctx.db.read() as s:
            ids = [a.id for a in s.scalars(select(PlatformAccount).where(PlatformAccount.project_id == project_id, PlatformAccount.status == AccountStatus.CONNECTED.value))]
        for aid in ids:
            with ctx.db.session() as s:
                acc = s.get(PlatformAccount, aid)
                adapter = ctx.accounts.adapter_for(acc)
                token = ctx.accounts.token_for(s, Actor.ai("metrics", project_id), acc, purpose="audience")
                ext = acc.external_id
            cnt = adapter.fetch_audience(ext, token) if hasattr(adapter, "fetch_audience") else None
            if cnt is not None:
                with ctx.db.session() as s:
                    s.add(AudienceSnapshot(account_id=aid, ts=ctx.clock.now(), followers=cnt))
                n += 1
        return n

    # ------------------------------------------------------------ ручной ввод / импорт
    def import_rows(self, s: Session, project_id: int, actor: Actor, rows: list[dict[str, Any]], *, source: str = "import") -> dict[str, Any]:
        """Telegram Bot API не отдаёт статистику постов — её можно внести вручную или импортом (CSV/JSON из аналитики канала)."""
        if source not in ("import", "manual"):
            raise ValidationFailed("source: import | manual")
        ok, errors = 0, []
        now = self.ctx.clock.now()
        for i, r in enumerate(rows[:1000]):
            pub = None
            if r.get("publication_id"):
                pub = s.get(Publication, int(r["publication_id"]))
            elif r.get("content_id") and r.get("platform"):
                c = s.scalars(select(Content).where(Content.content_id == r["content_id"], Content.project_id == project_id)).first()
                if c:
                    pub = s.scalars(select(Publication).where(Publication.content_pk == c.id, Publication.platform == r["platform"], Publication.state == PubState.PUBLISHED.value)).first()
            if pub is None or pub.project_id != project_id or pub.published_at is None:
                errors.append({"row": i + 1, "error": "публикация не найдена или не опубликована"})
                continue
            vals = {}
            for k in METRIC_FIELDS:
                if r.get(k) in (None, ""):
                    vals[k] = None
                    continue
                try:
                    v = int(r[k])
                except (TypeError, ValueError):
                    errors.append({"row": i + 1, "error": f"{k}: не число"})
                    break
                if v < 0:
                    errors.append({"row": i + 1, "error": f"{k}: отрицательное значение"})
                    break
                vals[k] = v
            else:
                captured = now
                if r.get("captured_at"):
                    try:
                        captured = datetime.fromisoformat(str(r["captured_at"]).replace("Z", "+00:00"))
                    except ValueError:
                        errors.append({"row": i + 1, "error": "captured_at: неверный формат"})
                        continue
                age = max(0.0, (captured - pub.published_at).total_seconds() / 3600)
                s.add(MetricSnapshot(publication_id=pub.id, captured_at=captured, age_hours=round(age, 2), unavailable=[k for k in METRIC_FIELDS if vals[k] is None], source=source, extra={}, **vals))
                ok += 1
        self.ctx.audit.log(s, actor, "metrics.import", project_id=project_id, details={"rows": len(rows), "imported": ok, "errors": len(errors), "source": source})
        return {"imported": ok, "errors": errors}

    # ------------------------------------------------------------ базовый уровень и оценка
    def latest_snapshot(self, s: Session, pub_id: int, *, min_age: float = 0.0) -> MetricSnapshot | None:
        q = select(MetricSnapshot).where(MetricSnapshot.publication_id == pub_id, MetricSnapshot.age_hours >= min_age).order_by(MetricSnapshot.age_hours.desc(), MetricSnapshot.id.desc()).limit(1)
        return s.scalars(q).first()

    def baseline(self, s: Session, project_id: int, platform: str, account_id: int | None, *, exclude_pub: int | None = None, n: int = 30) -> tuple[float | None, int]:
        q = select(Publication).where(Publication.project_id == project_id, Publication.platform == platform, Publication.state == PubState.PUBLISHED.value)
        if account_id:
            q = q.where(Publication.account_id == account_id)
        vals: list[float] = []
        for p in s.scalars(q.order_by(Publication.published_at.desc()).limit(n + 5)):
            if p.id == exclude_pub:
                continue
            snap = self.latest_snapshot(s, p.id, min_age=24)
            v = primary_value(snap) if snap else None
            if v:
                vals.append(v)
            if len(vals) >= n:
                break
        if len(vals) < 5:
            return None, len(vals)
        return statistics.median(vals), len(vals)

    def evaluate(self, project_id: int) -> list[dict[str, Any]]:
        """После 24 ч результат публикации сравнивается с базовым уровнем аккаунта → сигнал обучения и запись «прогноз vs факт»."""
        ctx = self.ctx
        out: list[dict[str, Any]] = []
        with ctx.db.session() as s:
            pubs = list(s.scalars(select(Publication).where(Publication.project_id == project_id, Publication.state == PubState.PUBLISHED.value)))
            for pub in pubs:
                if (pub.progress or {}).get("perf_recorded"):
                    continue
                snap = self.latest_snapshot(s, pub.id, min_age=24)
                val = primary_value(snap) if snap else None
                if not val:
                    continue
                base, n = self.baseline(s, project_id, pub.platform, pub.account_id, exclude_pub=pub.id)
                content = s.get(Content, pub.content_pk)
                pred = dict(content.prediction or {})
                rec = s.scalars(select(ForecastRecord).where(ForecastRecord.publication_id == pub.id)).first()
                if rec is None:
                    rec = ForecastRecord(project_id=project_id, content_pk=content.id, publication_id=pub.id, proposal_id=content.proposal_id, category=content.category, prediction=pred, created_at=ctx.clock.now())
                    s.add(rec)
                if base is None:
                    rec.actual = {"value": val, "baseline": None, "note": f"базовый уровень не сформирован (публикаций с метриками: {n} из 5 необходимых)"}
                    pub.progress = {**(pub.progress or {}), "perf_recorded": False}
                    continue
                log_ratio = math.log(val / base)
                confidence = min(1.0, n / 15)
                ev = None
                if content.event_id:
                    from ..db.models import Event

                    ev = s.get(Event, content.event_id)
                ctx.learning.record_performance(s, project_id, ev, content, log_ratio, confidence=confidence)
                predicted = self.predicted_log_ratio(s, project_id, pred)
                rec.actual = {"value": val, "baseline": base, "log_ratio": round(log_ratio, 3), "ratio": round(val / base, 2), "n_baseline": n, "platform": pub.platform}
                rec.error = {"predicted_log_ratio": round(predicted, 3), "abs_error": round(abs(predicted - log_ratio), 3), "signed": round(predicted - log_ratio, 3)}
                rec.evaluated_at = ctx.clock.now()
                pub.progress = {**(pub.progress or {}), "perf_recorded": True}
                out.append({"publication_id": pub.id, "ratio": round(val / base, 2)})
        return out

    # ---------------------------------------------------------------- прогноз
    def calibration(self, s: Session, project_id: int) -> tuple[float, float, int]:
        """Калибровка «интерес → log-отношение к базе» методом наименьших квадратов по оценённым публикациям (приор: наклон 0.5)."""
        xs, ys = [], []
        for r in s.scalars(select(ForecastRecord).where(ForecastRecord.project_id == project_id, ForecastRecord.evaluated_at.is_not(None))):
            if r.prediction.get("interest") is not None and r.actual.get("log_ratio") is not None:
                xs.append((float(r.prediction["interest"]) - 0.5) * 2)
                ys.append(float(r.actual["log_ratio"]))
        n = len(xs)
        if n < 6 or statistics.pvariance(xs) < 1e-6:
            return 0.5, 0.0, n
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum((x - mx) ** 2 for x in xs)
        w = n / (n + 6)  # усадка к приору при малой выборке
        return w * slope + (1 - w) * 0.5, my - slope * mx, n

    def predicted_log_ratio(self, s: Session, project_id: int, prediction: dict[str, Any]) -> float:
        slope, intercept, _n = self.calibration(s, project_id)
        return slope * (float(prediction.get("interest", 0.5)) - 0.5) * 2 + intercept

    def forecast_for(self, s: Session, project_id: int, prediction: dict[str, Any]) -> dict[str, Any]:
        slope, intercept, n = self.calibration(s, project_id)
        lr = slope * (float(prediction.get("interest", 0.5)) - 0.5) * 2 + intercept
        return {"expected_ratio": round(math.exp(lr), 2), "confidence": "низкая" if n < 10 else "средняя" if n < 30 else "высокая", "calibration_samples": n, "note": "Ожидание относительно обычного результата аккаунта; уточняется по фактическим результатам."}

    def forecast_accuracy(self, s: Session, project_id: int) -> dict[str, Any]:
        rows = [r for r in s.scalars(select(ForecastRecord).where(ForecastRecord.project_id == project_id, ForecastRecord.evaluated_at.is_not(None)))]
        if not rows:
            return {"n": 0, "note": "Пока нет публикаций с оценённым результатом (нужно ≥24 ч после публикации и базовый уровень по ≥5 постам)."}
        errs = [r.error["abs_error"] for r in rows]
        signed = [r.error["signed"] for r in rows]
        xs = [(float(r.prediction.get("interest", 0.5)) - 0.5) for r in rows]
        ys = [float(r.actual["log_ratio"]) for r in rows]
        corr = None
        if len(rows) >= 4 and statistics.pstdev(xs) > 1e-6 and statistics.pstdev(ys) > 1e-6:
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (len(xs) * statistics.pstdev(xs) * statistics.pstdev(ys))
        return {
            "n": len(rows), "mae_log": round(statistics.fmean(errs), 3), "bias_log": round(statistics.fmean(signed), 3), "correlation": round(corr, 2) if corr is not None else None,
            "points": [{"content_pk": r.content_pk, "publication_id": r.publication_id, "predicted": r.error["predicted_log_ratio"], "actual": r.actual["log_ratio"], "category": r.category} for r in rows[-50:]],
            "note": "log-отношение: 0 — как обычно, 0.69 ≈ вдвое лучше. Положительное смещение — система переоценивает темы.",
        }  # fmt: skip

    def post_stats(self, s: Session, project_id: int, pub_id: int) -> dict[str, Any]:
        pub = s.get(Publication, pub_id)
        if pub is None or pub.project_id != project_id:
            raise NotFound("Публикация не найдена")
        snaps = list(s.scalars(select(MetricSnapshot).where(MetricSnapshot.publication_id == pub.id).order_by(MetricSnapshot.age_hours, MetricSnapshot.id)))
        last = snaps[-1] if snaps else None
        base, n = self.baseline(s, project_id, pub.platform, pub.account_id, exclude_pub=pub.id)
        val = primary_value(last) if last else None
        return {
            "publication_id": pub.id, "platform": pub.platform, "published_at": pub.published_at.isoformat() if pub.published_at else None,
            "latest": {k: getattr(last, k) for k in METRIC_FIELDS} if last else None, "age_hours": last.age_hours if last else None, "unavailable": last.unavailable if last else [],
            "engagement_rate": engagement_rate(last) if last else None, "engagement_base": ("reach" if last and last.reach else "views" if last and last.views else None), "vs_baseline": round(val / base, 2) if (val and base) else None, "baseline_samples": n,
            "series": [{"age_hours": x.age_hours, "source": x.source, **{k: getattr(x, k) for k in METRIC_FIELDS}} for x in snaps],
            "note": "Данные платформ могут приходить с задержкой до 48 ч." if pub.platform == "instagram" else "",
        }  # fmt: skip
