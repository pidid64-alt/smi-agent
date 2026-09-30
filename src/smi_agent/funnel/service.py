"""Воронка отбора: пул → 50 → 15 → 10 проверенных → 5 предложений (ТЗ §11–15, §60)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.enums import VerificationStatus
from ..db.models import Event, FunnelItem, FunnelRun, Project, Proposal, Verification
from ..proposals.cards import build_card
from ..security.rbac import Actor
from ..settings_model import ProjectSettings, load_project_settings
from .repeat import annotate_repeats, event_similarity

log = logging.getLogger(__name__)
VERIF_FACTOR = {"multi_confirmed": 1.0, "confirmed": 0.97, "needs_check": 0.8}


@dataclass
class Cand:
    ev: Event
    score: float
    bucket: str
    reasons: list[str] = field(default_factory=list)


def select_balanced(items: list[Cand], n: int, kz_target: float, strength: float, *, max_per_category: int | None = None) -> list[Cand]:
    """Жадный отбор с «мягким» приором гео-доли: сильные кандидаты не вытесняются, слабые не добавляются ради квоты."""
    chosen: list[Cand] = []
    pool = sorted(items, key=lambda c: -c.score)
    targets = {"KZ": kz_target, "WORLD": 1 - kz_target}
    cats: dict[str, int] = {}
    while pool and len(chosen) < n:
        k = len(chosen)
        counts = {"KZ": sum(1 for c in chosen if c.bucket == "KZ"), "WORLD": sum(1 for c in chosen if c.bucket == "WORLD")}
        best, best_adj = None, -1.0
        for c in pool[:80]:
            if max_per_category and cats.get(c.ev.category, 0) >= max_per_category:
                continue
            cur_share = (counts[c.bucket] / k) if k else targets[c.bucket]
            adj = c.score * (1 + strength * (targets[c.bucket] - cur_share))
            if adj > best_adj:
                best, best_adj = c, adj
        if best is None:
            break
        chosen.append(best)
        cats[best.ev.category] = cats.get(best.ev.category, 0) + 1
        pool.remove(best)
    return chosen


def geo_share(items: list[Cand]) -> dict[str, Any]:
    kz = sum(1 for c in items if c.bucket == "KZ")
    total = len(items)
    return {"kz": kz, "world": total - kz, "kz_share": round(kz / total, 3) if total else None}


class FunnelService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---------------------------------------------------------------- запуск
    def run(self, s: Session, project_id: int, actor: Actor, *, trigger: str = "manual") -> FunnelRun:
        ctx = self.ctx
        now = ctx.clock.now()
        proj = s.get(Project, project_id)
        cfg = load_project_settings(proj.settings)
        fc = cfg.funnel
        run = FunnelRun(project_id=project_id, started_at=now, mode=cfg.mode.value, trigger=trigger, params=fc.model_dump(), status="running")
        s.add(run)
        s.flush()

        active = list(s.scalars(select(Event).where(
            Event.project_id == project_id, Event.merged_into_id.is_(None), Event.last_update_at >= now - timedelta(hours=max(fc.lookback_hours, fc.max_event_age_hours)),
        )))  # fmt: skip
        annotate_repeats(ctx, s, project_id, active)
        ctx.scoring.score_recent(s, project_id, event_ids=[e.id for e in active])
        for e in active:
            e.stage = "pool"

        items: list[FunnelItem] = []

        def log_item(stage: str, c: Cand, decision: str, rank: int = 0) -> None:
            items.append(FunnelItem(run_id=run.id, event_id=c.ev.id, stage=stage, rank=rank, score=c.score, geo_bucket=c.bucket, decision=decision, reasons=list(c.reasons)))

        recently_rejected = self._recently_rejected(s, project_id, now, fc.max_event_age_hours)
        # ---- этап 1: пул → 50
        cands = [Cand(e, e.trend_score, e.geo_bucket) for e in active]
        s1_ok: list[Cand] = []
        dropped_reasons: dict[str, int] = {}
        for c in cands:
            e = c.ev
            why: str | None = None
            age_h = (now - e.first_published_at).total_seconds() / 3600
            if e.phase == "stale" or age_h > fc.max_event_age_hours:
                why = "устарело"
            elif c.score < fc.min_trend_score:
                why = f"Trend Score {c.score:.0f} ниже порога {fc.min_trend_score:.0f}"
            elif (e.flags or {}).get("ad_share", 0) >= 0.6:
                why = "реклама/партнёрский материал"
            elif e.id in recently_rejected and e.n_independent < 2 * recently_rejected[e.id]:
                why = "ранее отклонено пользователем"
            elif (blocked := ctx.profile.is_blocked(s, project_id, e)) is not None:
                why = blocked
            if why:
                c.reasons.append(why)
                dropped_reasons[why.split(" ")[0]] = dropped_reasons.get(why.split(" ")[0], 0) + 1
                e.stage = "dropped" if e.stage == "pool" and why == "устарело" else e.stage
            else:
                s1_ok.append(c)
        s1 = select_balanced(s1_ok, fc.stage1, cfg.geo.kz_target, cfg.geo.balance_strength)
        s1_ids = {c.ev.id for c in s1}
        for i, c in enumerate(s1, 1):
            c.ev.stage = "s50"
            log_item("s50", c, "kept", i)
        for c in s1_ok:
            if c.ev.id not in s1_ids:
                c.reasons.append("не вошло в топ по рейтингу")
                log_item("s50", c, "dropped")
        for c in cands:
            if c.reasons and c.ev.id not in s1_ids and c not in s1_ok:
                log_item("s50", c, "dropped")

        # ---- этап 2: 50 → 15 (дубли, слабые, кликбейт, реклама, неподтверждённые, незначимые, повторы)
        s2_ok: list[Cand] = []
        for c in s1:
            e, v, f = c.ev, (c.ev.components or {}).get("values", {}), c.ev.flags or {}
            why = None
            if f.get("clickbait_share", 0) >= 0.5:
                why = "кликбейтная подача"
            elif f.get("ad_share", 0) >= 0.4:
                why = "признаки рекламы"
            elif f.get("hedge_share", 0) >= 0.6 and e.n_independent < 2 and not f.get("official_present"):
                why = "неподтверждённая информация (слухи)"
            elif len((e.features or {}).get("facts", [])) < fc.min_facts or v.get("fact_sufficiency", 0) < 0.3:
                why = "недостаточно фактов"
            elif max(v.get("significance", 0), v.get("practical_value", 0), v.get("discussion", 0)) < 0.25:
                why = "незначительное событие"
            elif f.get("repeat_kind") == "duplicate":
                why = f"повтор: уже публиковали ({f.get('repeat_of')})"
            elif f.get("injection") and e.n_independent < 2:
                why = "источник содержит подозрительные инструкции для ИИ"
            if why:
                c.reasons.append(why)
                log_item("s15", c, "dropped")
            else:
                s2_ok.append(c)
        # дубли между событиями
        s2_ok.sort(key=lambda c: -c.score)
        uniq: list[Cand] = []
        for c in s2_ok:
            dup = next((u for u in uniq if event_similarity(c.ev, u.ev) >= 0.5), None)
            if dup is not None:
                c.reasons.append(f"дубль события «{dup.ev.title[:40]}»")
                log_item("s15", c, "dropped")
            else:
                uniq.append(c)
        s2 = select_balanced(uniq, fc.stage2, cfg.geo.kz_target, cfg.geo.balance_strength)
        s2_ids = {c.ev.id for c in s2}
        for i, c in enumerate(s2, 1):
            c.ev.stage = "s15"
            log_item("s15", c, "kept", i)
        for c in uniq:
            if c.ev.id not in s2_ids:
                c.reasons.append("не вошло в 15 сильнейших")
                log_item("s15", c, "dropped")

        # ---- этап 3: 15 → 10 проверенных
        vers: dict[int, Verification] = {}
        s3_ok: list[Cand] = []
        unverified_left = fc.max_unverified_in_stage3
        for c in sorted(s2, key=lambda c: -c.score):
            ver = ctx.verification.verify_event(s, c.ev, run.id)
            vers[c.ev.id] = ver
            factor = VERIF_FACTOR.get(ver.status, 0.0)
            if ver.status == VerificationStatus.REJECTED.value:
                c.reasons.append("не прошло проверку: " + (ver.warnings[0] if ver.warnings else "противоречия/слухи"))
                c.ev.stage = "dropped"
                log_item("s10", c, "dropped")
                continue
            if ver.status == VerificationStatus.NEEDS_CHECK.value:
                if unverified_left <= 0:
                    c.reasons.append("требуется дополнительная проверка (лимит непроверенных исчерпан)")
                    log_item("s10", c, "dropped")
                    continue
                unverified_left -= 1
                c.reasons.append("требуется дополнительная проверка — допущено с предупреждением")
            c.score = round(c.score * factor, 2)
            s3_ok.append(c)
        s3 = select_balanced(s3_ok, fc.stage3, cfg.geo.kz_target, cfg.geo.balance_strength)
        s3_ids = {c.ev.id for c in s3}
        for i, c in enumerate(s3, 1):
            c.ev.stage = "s10"
            log_item("s10", c, "kept", i)
        for c in s3_ok:
            if c.ev.id not in s3_ids:
                c.reasons.append("не вошло в 10 проверенных")
                log_item("s10", c, "dropped")

        # ---- этап 4: 10 → 5 разнообразных предложений
        s4 = self.pick_final(s3, cfg, fc.final, exclude=set())
        ids4 = {c.ev.id for c in s4}
        s4.sort(key=lambda c: -c.score)
        s.execute(update(Proposal).where(Proposal.project_id == project_id, Proposal.status == "proposed").values(status="expired", resolved_at=now))
        proposals: list[Proposal] = []
        for slot, c in enumerate(s4, 1):
            c.ev.stage = "s5"
            log_item("s5", c, "kept", slot)
            card = build_card(s, c.ev, vers.get(c.ev.id), now, ctx.know, slot=slot)
            p = Proposal(
                project_id=project_id, run_id=run.id, slot=slot, event_id=c.ev.id, verification_id=vers[c.ev.id].id if c.ev.id in vers else None, card=card,
                prediction={"interest": c.ev.potential_interest, "value": c.ev.potential_value, "trend_score": c.ev.trend_score, "model": "v1"},
                status="proposed", shown_at=now,
            )  # fmt: skip
            s.add(p)
            proposals.append(p)
        for c in s3:
            if c.ev.id not in ids4:
                c.reasons.append("оставлено в резерве (разнообразие/порог)")
                log_item("s5", c, "dropped")
        for it in items:
            s.add(it)
        run.finished_at = ctx.clock.now()
        run.status = "done"
        run.counts = {"pool": len(active), "s50": len(s1), "s15": len(s2), "s10": len(s3), "s5": len(s4)}
        run.geo_ratio = {"target_kz": cfg.geo.kz_target, "s50": geo_share(s1), "s15": geo_share(s2), "s10": geo_share(s3), "s5": geo_share(s4)}
        run.notes = {"dropped_stage1": dropped_reasons, "unverified_allowed": fc.max_unverified_in_stage3 - unverified_left,
                     "note": "Слабые темы не добавлялись ради квоты: итоговое число может быть меньше целевого."}  # fmt: skip
        s.flush()
        ctx.audit.log(s, actor, "funnel.run", project_id=project_id, target_type="funnel_run", target_id=run.id, details={"trigger": trigger, "counts": run.counts})
        log.info("funnel project=%s counts=%s", project_id, run.counts)
        return run

    # ------------------------------------------------------------------ финал
    def pick_final(self, pool: list[Cand], cfg: ProjectSettings, n: int, *, exclude: set[int], existing: list[Cand] | None = None) -> list[Cand]:
        """5 предложений: сначала по лучшему кандидату для каждого направления (мягкий ориентир), затем — лучшие из оставшихся."""
        know = self.ctx.know
        floor = cfg.funnel.min_trend_score
        chosen: list[Cand] = list(existing or [])
        avail = [c for c in pool if c.ev.id not in exclude and c.ev.id not in {x.ev.id for x in chosen} and c.score >= floor]
        themes = know.themes

        def in_theme(c: Cand, th: dict[str, Any]) -> bool:
            if th.get("geo_bucket"):
                return c.bucket == th["geo_bucket"]
            return c.ev.category in th.get("categories", [])

        covered = {th["id"] for th in themes if any(in_theme(c, th) for c in chosen)}
        order = sorted((th for th in themes if th["id"] not in covered), key=lambda th: sum(1 for c in avail if in_theme(c, th)))
        for th in order:
            if len(chosen) >= n:
                break
            options = [c for c in avail if in_theme(c, th) and c.ev.id not in {x.ev.id for x in chosen}]
            if not options:
                continue
            best = max(options, key=lambda c: c.score * (1 + cfg.geo.balance_strength * self._geo_bonus(c, chosen, cfg)))
            best.reasons.append(f"направление: {th['name']}")
            chosen.append(best)
        # добор лучшими из оставшихся (не более 2 из одной категории)
        rest = [c for c in avail if c.ev.id not in {x.ev.id for x in chosen}]
        cats: dict[str, int] = {}
        for c in chosen:
            cats[c.ev.category] = cats.get(c.ev.category, 0) + 1
        rest = [c for c in rest if cats.get(c.ev.category, 0) < 2]
        extra = select_balanced(rest, n - len(chosen), cfg.geo.kz_target, cfg.geo.balance_strength, max_per_category=2)
        chosen.extend(extra)
        return chosen[:n]

    @staticmethod
    def _geo_bonus(c: Cand, chosen: list[Cand], cfg: ProjectSettings) -> float:
        k = len(chosen)
        target = cfg.geo.kz_target if c.bucket == "KZ" else 1 - cfg.geo.kz_target
        cur = (sum(1 for x in chosen if x.bucket == c.bucket) / k) if k else target
        return target - cur

    def _recently_rejected(self, s: Session, project_id: int, now, max_age_h: int) -> dict[int, int]:
        rows = s.execute(select(Proposal.event_id, Event.n_independent).join(Event, Event.id == Proposal.event_id).where(
            Proposal.project_id == project_id, Proposal.status.in_(["rejected", "replaced"]), Proposal.resolved_at >= now - timedelta(hours=max_age_h),
        ))  # fmt: skip
        return {r[0]: max(1, r[1]) for r in rows}
