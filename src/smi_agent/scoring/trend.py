"""Trend Score (ТЗ §8–9, §61.5): 13 компонентов, скорость роста по независимым источникам, фаза жизни события."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Event, EventSnapshot, Project
from ..settings_model import ProjectSettings, load_project_settings

PRACTICAL_CATEGORIES = {"economy", "finance", "realty", "health", "education", "law", "energy", "society"}
LOCAL_IMPACT_CATEGORIES = {"economy", "finance", "realty", "education", "health", "law", "society", "energy", "business"}


def sat(x: float, k: float) -> float:
    return 1.0 - math.exp(-max(0.0, x) / k)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


@dataclass
class ScoreResult:
    score: float
    values: dict[str, float]
    penalties: dict[str, float]
    explain: dict[str, str]
    velocity: float
    velocity_prev: float
    phase: str
    potential_interest: float
    potential_value: float
    notes: list[str] = field(default_factory=list)


def _ago(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))} мин назад"
    if hours < 48:
        return f"{hours:.0f} ч назад"
    return f"{hours / 24:.0f} дн назад"


def velocity_windows(times: list[datetime], now: datetime, window_h: float) -> tuple[float, float, int, int]:
    """Возвращает (v_now, v_prev, прирост сейчас, прирост до): независимые источники в час за два последних окна."""
    w = timedelta(hours=window_h)
    n_now = sum(1 for t in times if now - w < t <= now)
    n_prev = sum(1 for t in times if now - 2 * w < t <= now - w)
    return n_now / window_h, n_prev / window_h, n_now, n_prev


def classify_phase(*, age_first_h: float, age_last_h: float, n_ind: int, v_now: float, v_prev: float, max_age_h: float, freshness: float) -> str:
    if age_first_h > max_age_h or freshness < 0.08:
        return "stale"
    if n_ind <= 2 and age_first_h < 8:
        return "emerging"
    if v_now >= 1.0 and v_now >= 1.2 * v_prev:
        return "rising"
    if v_now >= 0.4 and v_now >= 0.6 * v_prev:
        return "peak"
    if n_ind >= 3 and v_now < v_prev:
        return "fading"
    if age_last_h > 18:
        return "fading"
    return "peak" if n_ind >= 4 else "emerging"


class TrendScorer:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def score_event(self, ev: Event, cfg: ProjectSettings, now: datetime, affinity: tuple[float, float, str] | None = None) -> ScoreResult:
        know = self.ctx.know
        sc = cfg.scoring
        f = ev.features or {}
        flags = ev.flags or {}
        carriers = f.get("carriers", [])
        times = [_parse(c["t"]) for c in carriers if not c.get("cited_only")] or [_parse(c["t"]) for c in carriers]
        text = f"{ev.title} {ev.summary}"
        age_first = max(0.0, (now - ev.first_published_at).total_seconds() / 3600)
        age_last = max(0.0, (now - ev.last_update_at).total_seconds() / 3600)
        hl = sc.freshness_half_life_hours
        v: dict[str, float] = {}
        ex: dict[str, str] = {}

        # 1. свежесть
        v["freshness"] = 0.6 * 0.5 ** (age_first / hl) + 0.4 * 0.5 ** (age_last / hl)
        ex["freshness"] = f"первая публикация {_ago(age_first)}, последнее обновление {_ago(age_last)}"

        # 2. независимые источники (вес — по надёжности; «заявленные» по ссылке — вполовину)
        n_eff = 0.0
        for c in carriers:
            w = c.get("r", 0.7) * (1.25 if c.get("tier") == "primary" else 1.0) * (0.5 if c.get("cited_only") else 1.0)
            n_eff += w
        v["independent_sources"] = sat(n_eff, sc.independent_saturation / 3)
        reprints = max(0, ev.n_articles - ev.n_independent)
        ex["independent_sources"] = f"{ev.n_independent} независимых источн. из {ev.n_articles} публикаций" + (f"; {reprints} перепечаток/обновлений не считаются" if reprints else "")

        # 3. скорость роста — только независимые источники (40 перепечаток ≠ значимость)
        vw = sc.velocity_window_hours
        v_now, v_prev, n_now, n_prev = velocity_windows(times, now, vw)
        if ev.n_independent <= 1:
            vel = 0.0
        else:
            vel = sat(v_now, 2.0)
            if v_now >= 1.0 and v_now >= 1.3 * v_prev and n_now >= 3:
                vel = min(1.0, vel + 0.12)
        v["velocity"] = vel
        ex["velocity"] = (
            f"за последние {vw:g} ч +{n_now} независимых источн. (до этого +{n_prev})" + (" — быстро набирает популярность" if v_now >= 1.0 and v_now >= 1.3 * v_prev and n_now >= 3 else "")
        )

        # 4. масштаб
        scale_hits = know.count("scale", text)
        nums = f.get("numbers", [])
        big = 0.0
        for n in nums:
            if n.get("u") in ("usd", "eur", "kzt", "rub", "cny"):
                val = float(n["v"]) * (1 if n["u"] in ("usd", "eur") else 0.002)
                big = max(big, 0.3 if val >= 1e9 else 0.2 if val >= 1e6 else 0.0)
            elif n.get("u") == "pct" and float(n["v"]) >= 10:
                big = max(big, 0.1)
            elif n.get("u") == "people" and float(n["v"]) >= 1e5:
                big = max(big, 0.25)
        countries = {c.get("c") for c in carriers if c.get("c")}
        v["scale"] = min(1.0, 0.3 * sat(scale_hits, 2.0) * 2 + big + 0.3 * min(1.0, max(0, len(countries) - 1) / 3) + 0.1 * sat(ev.n_independent, 4))
        ex["scale"] = f"маркеры масштаба: {scale_hits}, стран-источников: {len(countries)}" + (", крупные суммы/числа" if big else "")

        # 5. новизна (и защита от повторов §51: flags.repeat_sim выставляет модуль повторов)
        nov_hits = know.count("novelty", ev.title)
        follow = know.count("followup", text)
        repeat_sim = float(flags.get("repeat_sim", 0.0))
        v["novelty"] = max(0.0, min(1.0, 0.45 + 0.3 * sat(nov_hits, 1.5) - 0.3 * sat(follow, 1.0) + 0.25 * (1 - repeat_sim)))
        ex["novelty"] = "новая тема" if repeat_sim < 0.3 else f"похожая тема уже была недавно (сходство {repeat_sim:.0%})"

        # 6. значимость для КЗ и для мира
        kz_sig = 0.65 * ev.kz_relevance + (0.2 if ev.category in LOCAL_IMPACT_CATEGORIES and ev.kz_relevance > 0.4 else 0) + (0.15 if flags.get("official_present") and ev.kz_relevance > 0.4 else 0)
        foreign = len([c for c in carriers if c.get("c") and c["c"] != "KZ" and not c.get("cited_only")])
        world_sig = 0.55 * ev.world_relevance + 0.25 * min(1.0, foreign / 3) + 0.2 * v["scale"]
        kz_sig, world_sig = min(1.0, kz_sig), min(1.0, world_sig)
        v["kz_significance"], v["world_significance"] = kz_sig, world_sig
        v["significance"] = max(sc.kz_priority * kz_sig, sc.world_priority * world_sig)
        ex["significance"] = f"значимость для Казахстана {kz_sig:.0%}, для мира {world_sig:.0%}"

        # 7. практическая ценность
        pr_hits = know.count("practical", text)
        v["practical_value"] = min(1.0, 0.7 * sat(pr_hits, 2.5) + (0.3 if ev.category in PRACTICAL_CATEGORIES else 0.0) * (0.5 + 0.5 * sat(pr_hits, 1.0)))
        ex["practical_value"] = f"прикладные маркеры: {pr_hits}" + ("; тема влияет на повседневную жизнь" if ev.category in PRACTICAL_CATEGORIES else "")

        # 8/13. интерес аудитории и историческая эффективность — из профиля (нейтрально 0.5 без данных)
        aud, hist, note = affinity or (0.5, 0.5, "недостаточно данных профиля — нейтральная оценка")
        v["audience_interest"], v["historical"] = aud, hist
        ex["audience_interest"] = note
        ex["historical"] = "по результатам прошлых публикаций на похожие темы" if affinity else "пока нет результатов публикаций — нейтральная оценка"

        # 9. потенциал обсуждения
        ctr_hits = know.count("controversy", text)
        v["discussion"] = min(1.0, 0.5 * sat(ctr_hits, 2.0) + (0.2 if f.get("quotes") else 0) + (0.15 if flags.get("sensitive_category") else 0) + 0.15 * min(1.0, ev.n_sources / 6))
        ex["discussion"] = f"маркеры спора/реакции: {ctr_hits}"

        # 10. достаточность фактов
        facts = len(f.get("facts", []))
        numbers = len(nums)
        base = min(1.0, (min(facts, 4) / 4) * 0.7 + min(numbers, 3) / 3 * 0.2 + (0.1 if f.get("dates") else 0))
        base *= 1 - 0.6 * float(flags.get("hedge_share", 0))
        if flags.get("paywalled_only"):
            base *= 0.6
        v["fact_sufficiency"] = max(0.0, base)
        ex["fact_sufficiency"] = f"фактов: {facts}, чисел: {numbers}" + ("; есть оговорки «по слухам/якобы»" if flags.get("hedge_share") else "")

        # 11. качество источников
        wsum = sum(c.get("r", 0.7) * (0.5 if c.get("cited_only") else 1.0) for c in carriers)
        wcnt = sum(0.5 if c.get("cited_only") else 1.0 for c in carriers) or 1.0
        v["source_quality"] = min(1.0, wsum / wcnt + (0.12 if flags.get("official_present") else 0.0))
        ex["source_quality"] = "есть первичный/официальный источник" if flags.get("official_present") else "средняя надёжность источников"

        # 12. выполнимость (можно ли сделать качественный материал)
        feas = 0.3 + 0.35 * v["fact_sufficiency"] + 0.2 * (1.0 if (ev.features or {}).get("numbers") else 0.3) + 0.15 * (0.0 if flags.get("sensitive_category") else 1.0)
        if flags.get("injection"):
            feas -= 0.3
        v["feasibility"] = max(0.0, min(1.0, feas))
        ex["feasibility"] = "достаточно данных для оригинального материала" if v["feasibility"] >= 0.6 else "данных мало или тема чувствительная"

        weights = {k: float(w) for k, w in sc.weights.items()}
        wtotal = sum(weights.values()) or 1.0
        raw = sum(weights.get(k, 0.0) * v.get(k, 0.0) for k in weights) / wtotal * 100

        pens: dict[str, float] = {}
        if flags.get("clickbait_share"):
            pens["clickbait"] = 1 - 0.45 * flags["clickbait_share"]
        if flags.get("ad_share"):
            pens["ad"] = 1 - 0.7 * flags["ad_share"]
        if flags.get("hedge_share"):
            pens["hedge"] = 1 - 0.25 * flags["hedge_share"]
        if flags.get("opinion_share"):
            pens["opinion"] = 1 - 0.15 * flags["opinion_share"]
        if flags.get("injection"):
            pens["injection"] = 0.5
        if repeat_sim > 0.5:
            pens["repeat"] = 1 - min(0.5, (repeat_sim - 0.5))
        score = raw
        for m in pens.values():
            score *= m

        phase = classify_phase(age_first_h=age_first, age_last_h=age_last, n_ind=ev.n_independent, v_now=v_now, v_prev=v_prev, max_age_h=cfg.funnel.max_event_age_hours, freshness=v["freshness"])
        interest = min(1.0, 0.45 * aud + 0.3 * v["significance"] + 0.25 * v["discussion"])
        value = min(1.0, 0.6 * v["practical_value"] + 0.4 * v["significance"])
        return ScoreResult(round(score, 2), {k: round(x, 4) for k, x in v.items()}, {k: round(x, 3) for k, x in pens.items()}, ex, round(v_now, 3), round(v_prev, 3), phase, round(interest, 3), round(value, 3))

    def score_recent(self, s: Session, project_id: int, *, event_ids: list[int] | None = None) -> int:
        ctx = self.ctx
        now = ctx.clock.now()
        proj = s.get(Project, project_id)
        cfg = load_project_settings(proj.settings)
        q = select(Event).where(Event.project_id == project_id, Event.merged_into_id.is_(None))
        if event_ids is not None:
            q = q.where(Event.id.in_(event_ids))
        else:
            q = q.where(Event.last_update_at >= now - timedelta(hours=max(cfg.funnel.lookback_hours, cfg.funnel.max_event_age_hours)))
        profile = getattr(ctx, "profile", None)
        n = 0
        for ev in s.scalars(q):
            aff = profile.affinity(s, project_id, ev) if profile is not None else None
            res = self.score_event(ev, cfg, now, aff)
            ev.trend_score = res.score
            ev.velocity = res.velocity
            ev.phase = res.phase
            ev.potential_interest, ev.potential_value = res.potential_interest, res.potential_value
            ev.components = {"values": res.values, "penalties": res.penalties, "explain": res.explain, "weights": cfg.scoring.weights, "velocity_prev": res.velocity_prev}
            ev.last_scored_at = now
            snap = s.scalars(select(EventSnapshot).where(EventSnapshot.event_id == ev.id).order_by(EventSnapshot.ts.desc()).limit(1)).first()
            if snap is not None and abs((now - snap.ts).total_seconds()) < 120:
                snap.trend_score, snap.velocity = ev.trend_score, ev.velocity
            n += 1
        s.flush()
        return n
