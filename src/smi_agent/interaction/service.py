"""Взаимодействие с предложениями (ТЗ §17, §61.9): выбрать, отклонить, заменить, раскрыть подробнее, сменить угол; каждое действие — сигнал обучения."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import ActionKind
from ..core.errors import NotFound, ValidationFailed
from ..db.models import Article, Event, FunnelItem, Project, Proposal, UserAction, Verification
from ..funnel.service import Cand
from ..proposals.cards import CATEGORY_ANGLES, DEFAULT_ANGLE, build_card
from ..security.rbac import Actor
from ..settings_model import load_project_settings
from .commands import Command, parse_commands

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    slot: int
    action: str
    ok: bool
    message: str
    content_id: str | None = None
    proposal_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


class InteractionService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------ вспомогательное
    def current(self, s: Session, project_id: int) -> list[Proposal]:
        """Актуальные предложения: последние по слоту среди ещё не просроченных/не замененных."""
        rows = list(s.scalars(select(Proposal).where(Proposal.project_id == project_id, Proposal.status.in_(["proposed", "selected", "executed"])).order_by(Proposal.run_id.desc(), Proposal.slot)))
        if not rows:
            return []
        latest_run = max((p.run_id or 0) for p in rows)
        return sorted([p for p in rows if (p.run_id or 0) == latest_run], key=lambda p: p.slot)

    def _by_slot(self, s: Session, project_id: int, slot: int) -> Proposal:
        for p in self.current(s, project_id):
            if p.slot == slot:
                return p
        raise NotFound(f"Нет предложения №{slot}")

    def _log(self, s: Session, project_id: int, actor: Actor, kind: ActionKind, p: Proposal | None, raw: str, payload: dict[str, Any]) -> None:
        uid = int(actor.id) if actor.type == "user" and actor.id.isdigit() else None
        s.add(UserAction(project_id=project_id, user_id=uid, proposal_id=p.id if p else None, event_id=p.event_id if p else None, kind=kind.value, payload=payload, raw_text=raw[:500], created_at=self.ctx.clock.now()))
        s.flush()

    # ------------------------------------------------------------------ текстовые команды
    def handle_text(self, s: Session, project_id: int, actor: Actor, text: str) -> dict[str, Any]:
        parsed = parse_commands(text)
        results: list[ActionResult] = []
        for cmd in parsed.commands:
            try:
                results.append(self.execute(s, project_id, actor, cmd))
            except (NotFound, ValidationFailed) as e:
                results.append(ActionResult(cmd.slot, cmd.kind.value, False, e.message))
        return {"results": [r.__dict__ for r in results], "hint": parsed.hint, "unrecognized": parsed.unrecognized}

    def execute(self, s: Session, project_id: int, actor: Actor, cmd: Command) -> ActionResult:
        if cmd.kind in (ActionKind.SELECT, ActionKind.MODIFY):
            return self.select(s, project_id, actor, cmd.slot, cmd.params, raw=cmd.raw)
        if cmd.kind == ActionKind.REJECT:
            return self.reject(s, project_id, actor, cmd.slot, cmd.params.get("reason", ""), raw=cmd.raw)
        if cmd.kind == ActionKind.REPLACE:
            return self.replace(s, project_id, actor, cmd.slot, raw=cmd.raw)
        if cmd.kind == ActionKind.MORE_INFO:
            return self.more_info(s, project_id, actor, cmd.slot, raw=cmd.raw)
        if cmd.kind == ActionKind.CHANGE_ANGLE:
            return self.change_angle(s, project_id, actor, cmd.slot, cmd.params.get("angle"), raw=cmd.raw)
        raise ValidationFailed("Неподдерживаемая команда")

    # ------------------------------------------------------------------ действия
    def select(self, s: Session, project_id: int, actor: Actor, slot: int, overrides: dict[str, Any] | None = None, *, raw: str = "") -> ActionResult:
        p = self._by_slot(s, project_id, slot)
        if p.status in ("selected", "executed") and p.content_pk:
            raise ValidationFailed(f"№{slot} уже выбрано — материал создан")
        overrides = {k: v for k, v in (overrides or {}).items() if k in ("emphasis", "emphasis_text", "language", "format", "length", "tone", "platforms", "angle")}
        ev = s.get(Event, p.event_id)
        siblings = [(sp, s.get(Event, sp.event_id)) for sp in self.current(s, project_id)]
        content = self.ctx.content.create_from_proposal(s, p, overrides, actor)
        p.status, p.resolved_at, p.resolved_by, p.overrides = "selected", self.ctx.clock.now(), actor.label, overrides
        self.ctx.learning.record_selection(s, project_id, p, ev, siblings, overrides=overrides)
        self._log(s, project_id, actor, ActionKind.MODIFY if overrides else ActionKind.SELECT, p, raw or f"Беру №{slot}", {"overrides": overrides, "content_id": content.content_id})
        self.ctx.audit.log(s, actor, "proposal.select", project_id=project_id, target_type="proposal", target_id=p.id, details={"slot": slot, "content_id": content.content_id, "overrides": overrides})
        msg = f"Беру №{slot}: «{ev.title[:80]}». Черновик {content.content_id} подготовлен для: {', '.join(v.platform for v in self.ctx.content.current_versions(s, content.id))}."
        if overrides.get("emphasis_text"):
            msg += f" Акцент: {overrides['emphasis_text']}."
        return ActionResult(slot, "select", True, msg, content.content_id, p.id, {"content_pk": content.id})

    def reject(self, s: Session, project_id: int, actor: Actor, slot: int, reason: str = "", *, raw: str = "") -> ActionResult:
        p = self._by_slot(s, project_id, slot)
        if p.status != "proposed":
            raise ValidationFailed(f"№{slot} уже обработано ({p.status})")
        ev = s.get(Event, p.event_id)
        p.status, p.resolved_at, p.resolved_by = "rejected", self.ctx.clock.now(), actor.label
        self.ctx.learning.record_rejection(s, project_id, p, ev, reason)
        self._log(s, project_id, actor, ActionKind.REJECT, p, raw or f"№{slot} неинтересна", {"reason": reason})
        self.ctx.audit.log(s, actor, "proposal.reject", project_id=project_id, target_type="proposal", target_id=p.id, details={"slot": slot, "reason": reason})
        return ActionResult(slot, "reject", True, f"№{slot} отклонена" + (f" (причина: {reason})" if reason else "") + ". Учту при следующих подборках.", proposal_id=p.id)

    def replace(self, s: Session, project_id: int, actor: Actor, slot: int, *, raw: str = "") -> ActionResult:
        p = self._by_slot(s, project_id, slot)
        if p.status != "proposed":
            raise ValidationFailed(f"№{slot} уже обработано ({p.status})")
        cfg = load_project_settings(s.get(Project, project_id).settings)
        ev = s.get(Event, p.event_id)
        shown = {x.event_id for x in s.scalars(select(Proposal).where(Proposal.run_id == p.run_id))}
        q = s.execute(select(FunnelItem).where(FunnelItem.run_id == p.run_id, FunnelItem.stage.in_(["s10", "s5"]), FunnelItem.event_id.not_in(shown)).order_by(FunnelItem.score.desc()))
        floor = cfg.funnel.min_trend_score
        cands: list[Cand] = []
        seen: set[int] = set()
        for it in q.scalars():
            if it.event_id in seen:
                continue
            seen.add(it.event_id)
            e2 = s.get(Event, it.event_id)
            if e2 is None or e2.verification_status == "rejected" or e2.trend_score < floor or e2.stage == "dropped":
                continue
            cands.append(Cand(e2, e2.trend_score, e2.geo_bucket))
        if not cands:
            raise ValidationFailed("Достойной замены нет: оставшиеся темы ниже порога значимости или не прошли проверку. Слабые темы не добавляются ради заполнения списка.")
        same_theme = [c for c in cands if c.ev.category == ev.category or c.bucket == ev.geo_bucket]
        best = max(same_theme or cands, key=lambda c: c.score)
        ver = s.scalars(select(Verification).where(Verification.event_id == best.ev.id).order_by(Verification.id.desc()).limit(1)).first()
        card = build_card(s, best.ev, ver, self.ctx.clock.now(), self.ctx.know, slot=slot)
        new = Proposal(
            project_id=project_id, run_id=p.run_id, slot=slot, event_id=best.ev.id, verification_id=ver.id if ver else None, card=card,
            prediction={"interest": best.ev.potential_interest, "value": best.ev.potential_value, "trend_score": best.ev.trend_score, "model": "v1"}, status="proposed", shown_at=self.ctx.clock.now(),
        )  # fmt: skip
        s.add(new)
        s.flush()
        p.status, p.resolved_at, p.resolved_by, p.replaced_by_id = "replaced", self.ctx.clock.now(), actor.label, new.id
        best.ev.stage = "s5"
        self.ctx.learning.record_replace(s, project_id, p, ev)
        self._log(s, project_id, actor, ActionKind.REPLACE, p, raw or f"Замени №{slot}", {"new_event_id": best.ev.id})
        self.ctx.audit.log(s, actor, "proposal.replace", project_id=project_id, target_type="proposal", target_id=p.id, details={"slot": slot, "new_proposal": new.id})
        return ActionResult(slot, "replace", True, f"№{slot} заменена: «{best.ev.title[:80]}».", proposal_id=new.id, data={"card": card})

    def more_info(self, s: Session, project_id: int, actor: Actor, slot: int, *, raw: str = "") -> ActionResult:
        p = self._by_slot(s, project_id, slot)
        ev = s.get(Event, p.event_id)
        ver = s.get(Verification, p.verification_id) if p.verification_id else None
        arts = list(s.scalars(select(Article).where(Article.event_id == ev.id).order_by(Article.published_at)))
        card = p.card or {}
        angles = [card.get("angle") or DEFAULT_ANGLE, "Сухие факты за 30 секунд: что, где, когда и откуда данные"]
        if ev.kz_relevance > 0.3:
            angles.append("Почему это важно именно для Казахстана (только подтверждённые связи)")
        if (ev.features or {}).get("dates"):
            angles.append("Что дальше: известные сроки и шаги без прогнозов")
        if len((ev.features or {}).get("numbers", [])) >= 2:
            angles.append("Объясняем на цифрах: что измерено и как это менялось")
        expanded = {
            "facts": [{"text": f["text"], "support": f.get("support", 1), "source": f.get("source")} for f in (ver.facts if ver else (ev.features or {}).get("facts", []))],
            "sources": [{"name": a.source.name, "url": a.url, "relation": a.relation, "independent": a.independent, "published_at": a.published_at.isoformat(), "excerpt": (a.summary or a.body)[:240]} for a in arts[:12]],
            "verification": {"status": ev.verification_status, "checks": ver.checks if ver else [], "warnings": ver.warnings if ver else [], "contradictions": ver.contradictions if ver else []},
            "timeline": self.ctx.events.timeline(s, ev), "angles": angles[:5], "unknowns": (ver.warnings if ver else [])[:5],
            "score_explain": (ev.components or {}).get("explain", {}),
        }  # fmt: skip
        p.expanded = expanded
        self.ctx.learning.record_more_info(s, project_id, p, ev)
        self._log(s, project_id, actor, ActionKind.MORE_INFO, p, raw or f"Раскрой №{slot} подробнее", {})
        return ActionResult(slot, "more_info", True, f"Подробности по №{slot}: {len(expanded['facts'])} фактов, {len(expanded['sources'])} публикаций, {len(expanded['angles'])} вариантов подачи.", proposal_id=p.id, data=expanded)

    def change_angle(self, s: Session, project_id: int, actor: Actor, slot: int, angle: str | None, *, raw: str = "") -> ActionResult:
        p = self._by_slot(s, project_id, slot)
        ev = s.get(Event, p.event_id)
        options = (p.expanded or {}).get("angles") or [CATEGORY_ANGLES.get(ev.category, DEFAULT_ANGLE), "Сухие факты за 30 секунд: что, где, когда и откуда данные"]
        cur = (p.overrides or {}).get("angle") or (p.card or {}).get("angle")
        if angle:
            new = angle
        else:
            idx = options.index(cur) if cur in options else -1
            new = options[(idx + 1) % len(options)]
        p.overrides = {**(p.overrides or {}), "angle": new}
        card = dict(p.card or {})
        card["angle"] = new
        p.card = card
        self._log(s, project_id, actor, ActionKind.CHANGE_ANGLE, p, raw or f"Другой угол для №{slot}", {"angle": new})
        return ActionResult(slot, "change_angle", True, f"Угол для №{slot}: {new}", proposal_id=p.id, data={"angle": new})
