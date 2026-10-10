"""Планирование (ТЗ §26, §61.16): «Оптимальное время», тихие часы, интервалы между постами, дневные лимиты."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.enums import PubState
from ..core.errors import ValidationFailed
from ..db.models import MetricSnapshot, Project, Publication
from ..settings_model import ProjectSettings, load_project_settings

ACTIVE_STATES = (PubState.SCHEDULED.value, PubState.PUBLISHING.value, PubState.PUBLISHED.value)


def tz_of(cfg: ProjectSettings) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.scheduling.timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def in_quiet_hours(cfg: ProjectSettings, dt: datetime) -> bool:
    start, end = cfg.scheduling.quiet_hours
    h = dt.astimezone(tz_of(cfg)).hour
    return (start <= h < end) if start <= end else (h >= start or h < end)


class SchedulingService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # --------------------------------------------------------------- статистика по часам
    def hour_scores(self, s: Session, project_id: int, platform: str, cfg: ProjectSettings) -> tuple[dict[int, float], int]:
        """Средний логарифм отношения результата к среднему по платформе для каждого локального часа публикации."""
        tz = tz_of(cfg)
        rows = s.execute(
            select(Publication.published_at, func.max(MetricSnapshot.views), func.max(MetricSnapshot.reach), func.max(MetricSnapshot.likes), func.max(MetricSnapshot.comments), func.max(MetricSnapshot.shares))
            .join(MetricSnapshot, MetricSnapshot.publication_id == Publication.id)
            .where(Publication.project_id == project_id, Publication.platform == platform, Publication.state == PubState.PUBLISHED.value, Publication.published_at.is_not(None), MetricSnapshot.age_hours >= 12)
            .group_by(Publication.id)
        ).all()
        vals = []
        for pub_at, views, reach, likes, comments, shares in rows:
            score = views or reach or ((likes or 0) + 2 * (comments or 0) + 3 * (shares or 0))
            if score and score > 0:
                vals.append((pub_at.astimezone(tz).hour, math.log(score)))
        if len(vals) < 8:
            return {}, len(vals)
        mean = sum(v for _, v in vals) / len(vals)
        by_hour: dict[int, list[float]] = {}
        for h, v in vals:
            by_hour.setdefault(h, []).append(v - mean)
        return {h: (sum(xs) / len(xs)) * (len(xs) / (len(xs) + 2)) for h, xs in by_hour.items()}, len(vals)

    # ------------------------------------------------------------------ ограничения
    def last_time(self, s: Session, project_id: int, platform: str, now: datetime, *, account_id: int | None = None) -> list[datetime]:
        q = select(Publication.scheduled_at, Publication.published_at).where(Publication.project_id == project_id, Publication.platform == platform, Publication.state.in_(ACTIVE_STATE_LIST))
        if account_id:
            q = q.where(Publication.account_id == account_id)
        out = []
        for sched, pub in s.execute(q.order_by(Publication.id.desc()).limit(50)):
            t = pub or sched
            if t:
                out.append(t)
        return out

    def count_today(self, s: Session, project_id: int, cfg: ProjectSettings, now: datetime, *, origin: str | None = None, platform: str | None = None) -> int:
        tz = tz_of(cfg)
        start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        q = select(func.count()).select_from(Publication).where(Publication.project_id == project_id, Publication.state.in_(ACTIVE_STATE_LIST), func.coalesce(Publication.published_at, Publication.scheduled_at) >= start)
        if origin:
            q = q.where(Publication.origin == origin)
        if platform:
            q = q.where(Publication.platform == platform)
        return s.scalar(q) or 0

    # ----------------------------------------------------------------- время
    def optimal_time(self, s: Session, project_id: int, platform: str, now: datetime, *, account_id: int | None = None, min_gap_min: int = 30) -> tuple[datetime, str]:
        cfg = load_project_settings(s.get(Project, project_id).settings)
        tz = tz_of(cfg)
        scores, n = self.hour_scores(s, project_id, platform, cfg)
        defaults = cfg.scheduling.default_hours.get(platform, [10, 13, 19])
        if scores:
            ranked = sorted(scores, key=lambda h: -scores[h])
            hours = ranked[:4]
            why = f"лучшие часы по вашей статистике ({n} публикаций): " + ", ".join(f"{h:02d}:00" for h in hours[:3])
        else:
            hours = list(defaults)
            why = f"по умолчанию для платформы (данных статистики пока мало: {n}): " + ", ".join(f"{h:02d}:00" for h in hours[:3])
        existing = self.last_time(s, project_id, platform, now, account_id=account_id)
        local_now = now.astimezone(tz)
        cands: list[tuple[int, datetime]] = []
        for day in range(0, 3):
            for rank, h in enumerate(hours):
                dt = (local_now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(days=day)).astimezone(UTC)
                if dt < now + timedelta(minutes=5) or in_quiet_hours(cfg, dt):
                    continue
                if any(abs((dt - t).total_seconds()) < min_gap_min * 60 for t in existing):
                    continue
                cands.append((day * 10 + rank, dt))
        if not cands:
            raise ValidationFailed("Не удалось подобрать свободное время в ближайшие 3 дня")
        cands.sort(key=lambda x: (x[1] - now).total_seconds() if x[0] < 10 else 10**9 + x[0])
        best = min(cands, key=lambda x: x[1])[1] if any(c[0] < 10 for c in cands) else cands[0][1]
        return best, why

    def resolve(self, s: Session, project_id: int, platform: str, spec: dict[str, Any] | None, now: datetime, *, account_id: int | None = None, autonomous: bool = False) -> tuple[datetime, dict[str, Any]]:
        spec = spec or {"mode": "optimal"}
        mode = spec.get("mode", "optimal")
        cfg = load_project_settings(s.get(Project, project_id).settings)
        if mode == "now":
            return now, {"mode": "now", "reason": "публикация сразу после подтверждения"}
        if mode == "at":
            try:
                dt = datetime.fromisoformat(str(spec["at"]).replace("Z", "+00:00"))
            except (KeyError, ValueError) as e:
                raise ValidationFailed("Некорректная дата/время публикации") from e
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz_of(cfg))
            dt = dt.astimezone(UTC)
            if dt < now - timedelta(minutes=1):
                raise ValidationFailed("Время публикации уже прошло")
            if autonomous and in_quiet_hours(cfg, dt):
                raise ValidationFailed("Автопилот не публикует в тихие часы")
            return dt, {"mode": "at", "reason": "время задано вручную"}
        if mode == "optimal":
            gap = cfg.autopilot.min_gap_minutes if autonomous else 30
            dt, why = self.optimal_time(s, project_id, platform, now, account_id=account_id, min_gap_min=gap)
            return dt, {"mode": "optimal", "reason": why}
        raise ValidationFailed("Режим времени: now | at | optimal")


ACTIVE_STATE_LIST = list(ACTIVE_STATES)
