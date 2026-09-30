"""Автопилот и со-редактор (ТЗ §39, §52, §61): по умолчанию ВЫКЛЮЧЕН; включается отдельно, только в рамках политик и проверок.

Политика/чувствительные темы — только с ручным подтверждением. Любая непройденная проверка запрещает автопубликацию.
Выбор автопилота не считается сигналом предпочтений пользователя (чтобы не возникало петли самоподкрепления).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import AccountStatus, AgentMode, PublishMode, PubState
from ..core.errors import ValidationFailed
from ..db.models import AutopilotPolicy, Event, Notification, PlatformAccount, Project, Proposal, Publication
from ..security.rbac import Actor, Perm, authorize
from ..settings_model import load_project_settings
from .scheduling import in_quiet_hours

log = logging.getLogger(__name__)
VERIF_RANK = {"needs_check": 0, "confirmed": 1, "multi_confirmed": 2}
SAFE_FORMATS = {"post_card", "short_post", "carousel_numbers"}  # без Reels: видео система не генерирует


class AutopilotService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------ политики
    def get_policy(self, s: Session, project_id: int, platform: str | None = None, category: str | None = None) -> AutopilotPolicy | None:
        return s.scalars(select(AutopilotPolicy).where(AutopilotPolicy.project_id == project_id, AutopilotPolicy.platform.is_(None) if platform is None else AutopilotPolicy.platform == platform, AutopilotPolicy.category.is_(None) if category is None else AutopilotPolicy.category == category)).first()

    def policies(self, s: Session, project_id: int) -> list[AutopilotPolicy]:
        return list(s.scalars(select(AutopilotPolicy).where(AutopilotPolicy.project_id == project_id).order_by(AutopilotPolicy.id)))

    def set_policy(self, s: Session, actor: Actor, project_id: int, *, enabled: bool, platform: str | None = None, category: str | None = None, constraints: dict[str, Any] | None = None) -> AutopilotPolicy:
        authorize(actor, Perm.AUTOPILOT_MANAGE)
        if category and self.ctx.know.is_sensitive_category(category) and enabled:
            raise ValidationFailed("Для чувствительных категорий (политика, происшествия) автопубликация запрещена")
        pol = self.get_policy(s, project_id, platform, category)
        if pol is None:
            pol = AutopilotPolicy(project_id=project_id, platform=platform, category=category, mode=PublishMode.AUTO.value)
            s.add(pol)
        pol.enabled, pol.constraints, pol.updated_at, pol.updated_by = enabled, constraints or pol.constraints or {}, self.ctx.clock.now(), actor.label
        s.flush()
        self.ctx.audit.log(s, actor, "autopilot.policy", project_id=project_id, target_type="autopilot_policy", target_id=pol.id, details={"enabled": enabled, "platform": platform, "category": category, "constraints": pol.constraints})
        return pol

    def set_mode(self, s: Session, actor: Actor, project_id: int, mode: str) -> dict[str, Any]:
        """Режим агента (ТЗ §52): learning | co_editor | autopilot. Автопилот включает только администратор и только осознанно."""
        authorize(actor, Perm.SETTINGS_EDIT)
        try:
            new = AgentMode(mode)
        except ValueError as e:
            raise ValidationFailed("Режим: learning | co_editor | autopilot") from e
        if new == AgentMode.AUTOPILOT:
            authorize(actor, Perm.AUTOPILOT_MANAGE)
        proj = s.get(Project, project_id)
        settings = dict(proj.settings or {})
        old = settings.get("mode", AgentMode.LEARNING.value)
        settings["mode"] = new.value
        load_project_settings(settings)  # валидация
        proj.settings = settings
        self.ctx.audit.log(s, actor, "agent.mode", project_id=project_id, details={"from": old, "to": new.value})
        return {"mode": new.value, "previous": old}

    # ------------------------------------------------------------------- цикл
    def tick(self, project_id: int) -> dict[str, Any]:
        """Один цикл. Фазы: (1) чтение и отбор, (2) генерация текста вне транзакции записи, (3) короткая запись."""
        ctx = self.ctx
        now = ctx.clock.now()
        actor = Actor.ai("autopilot", project_id)
        with ctx.db.read() as rs:
            cfg = load_project_settings(rs.get(Project, project_id).settings)
            rec: dict[str, Any] = {"at": now.isoformat(), "mode": cfg.mode.value, "actions": [], "skipped": []}
            if cfg.mode == AgentMode.LEARNING:
                rec["status"] = "off"
                return rec
            plan = self._plan_co_editor(rs, project_id, rec) if cfg.mode == AgentMode.CO_EDITOR else self._plan_autopilot(rs, project_id, cfg, rec, now)
        candidates: list[dict[str, Any]] = plan.get("candidates", [])
        tried = 0
        for cand in candidates[:3]:
            tried += 1
            with ctx.db.read() as rs:  # фаза 2: LLM — вне транзакции записи
                prep = ctx.content.prepare(rs, cand["proposal_id"], cand["overrides"])
            with ctx.db.session() as s:  # фаза 3: запись
                done = self._commit(s, project_id, cfg, actor, rec, cand, prep, now, co_editor=cfg.mode == AgentMode.CO_EDITOR)
            if done:
                break
        with ctx.db.session() as s:
            return self._finish(s, actor, project_id, rec, plan.get("status") if not candidates else ("drafted" if cfg.mode == AgentMode.CO_EDITOR and rec["actions"] else "scheduled" if rec["actions"] else "nothing_suitable"))

    def _finish(self, s: Session, actor: Actor, project_id: int, rec: dict[str, Any], status: str) -> dict[str, Any]:
        rec["status"] = status or "idle"
        if rec["actions"] or rec["skipped"]:
            self.ctx.audit.log(s, actor, "autopilot.decision", project_id=project_id, outcome="ok" if rec["actions"] else "skipped", details={"status": rec["status"], "actions": rec["actions"][:6], "skipped": rec["skipped"][:8]})
        return rec

    # ---- фаза 1 (только чтение)
    def _plan_co_editor(self, s: Session, project_id: int, rec: dict[str, Any]) -> dict[str, Any]:
        cur = self.ctx.interaction.current(s, project_id)
        props = [p for p in cur if p.status == "proposed" and not p.autopilot]
        if not props:
            rec["skipped"].append("нет новых предложений")
            return {"status": "idle"}
        if any(x.autopilot and x.status == "selected" for x in cur):
            rec["skipped"].append("черновик для этого подбора уже подготовлен")
            return {"status": "idle"}
        p = max(props, key=lambda x: (x.card or {}).get("trend_score", 0))
        fmt = ((p.card or {}).get("format") or {}).get("code")
        return {"candidates": [{"proposal_id": p.id, "overrides": {"format": fmt} if fmt in SAFE_FORMATS else {}, "platforms": None}]}

    def _plan_autopilot(self, s: Session, project_id: int, cfg: Any, rec: dict[str, Any], now) -> dict[str, Any]:
        ctx = self.ctx
        ap = cfg.autopilot
        if ctx.killswitch.blocking(s, project_id) is not None:
            rec["skipped"].append("автопилот остановлен аварийным выключателем")
            return {"status": "killed"}
        if ap.require_llm_generator and not ctx.llm.enabled:
            rec["skipped"].append("LLM не настроена: автопубликация текстов-заготовок запрещена")
            return {"status": "blocked"}
        if in_quiet_hours(cfg, now):
            rec["skipped"].append("тихие часы")
            return {"status": "quiet"}
        global_policy = self.get_policy(s, project_id)
        accounts = list(s.scalars(select(PlatformAccount).where(PlatformAccount.project_id == project_id, PlatformAccount.status == AccountStatus.CONNECTED.value, PlatformAccount.mode == PublishMode.AUTO.value)))
        eligible: list[PlatformAccount] = []
        for a in accounts:
            pol = self.get_policy(s, project_id, a.platform) or global_policy
            if pol is None or not pol.enabled:
                rec["skipped"].append(f"{a.platform}: политика автопилота не включена")
            elif ctx.killswitch.blocking(s, project_id, platform=a.platform, account_id=a.id) is not None:
                rec["skipped"].append(f"{a.platform}: остановлено выключателем")
            else:
                eligible.append(a)
        if not eligible:
            return {"status": "no_accounts"}
        if ctx.scheduling.count_today(s, project_id, cfg, now, origin="autopilot") >= ap.max_posts_per_day:
            rec["skipped"].append(f"достигнут дневной лимит автопилота ({ap.max_posts_per_day})")
            return {"status": "daily_cap"}
        last = s.scalars(select(Publication).where(Publication.project_id == project_id, Publication.origin == "autopilot", Publication.state.in_([PubState.SCHEDULED.value, PubState.PUBLISHING.value, PubState.PUBLISHED.value])).order_by(Publication.id.desc()).limit(1)).first()
        if last is not None:
            t = last.published_at or last.scheduled_at
            if t and abs((now - t).total_seconds()) < ap.min_gap_minutes * 60:
                rec["skipped"].append(f"минимальный интервал между постами ({ap.min_gap_minutes} мин)")
                return {"status": "gap"}
        props = [p for p in ctx.interaction.current(s, project_id) if p.status == "proposed" and not p.autopilot]
        props.sort(key=lambda p: -((p.card or {}).get("trend_score", 0)))
        if not props:
            rec["skipped"].append("нет предложений для автопубликации")
            return {"status": "idle"}
        cands: list[dict[str, Any]] = []
        for p in props:
            ev = s.get(Event, p.event_id)
            why = self._gate(s, p, ev, cfg, global_policy)
            if why:
                rec["skipped"].append({"slot": p.slot, "title": ev.title[:60], "reasons": why})
                continue
            fmt = ((p.card or {}).get("format") or {}).get("code")
            cands.append({"proposal_id": p.id, "overrides": {**({} if fmt in SAFE_FORMATS else {"format": "post_card"}), "platforms": [a.platform for a in eligible]}, "platforms": [a.platform for a in eligible]})
        return {"candidates": cands, "status": "nothing_suitable"}

    # ---- фаза 3 (запись)
    def _commit(self, s: Session, project_id: int, cfg: Any, actor: Actor, rec: dict[str, Any], cand: dict[str, Any], prep: Any, now, *, co_editor: bool) -> bool:
        ctx = self.ctx
        p = s.get(Proposal, cand["proposal_id"])
        if p.status != "proposed" or p.autopilot:
            return False
        ev = s.get(Event, p.event_id)
        content = ctx.content.persist(s, p, prep, actor, origin="co_editor" if co_editor else "autopilot")
        p.autopilot, p.resolved_by = True, actor.label
        if co_editor:
            p.status = "selected"
            pubs = ctx.publishing.create_for_content(s, project_id, content.id, actor, origin="co_editor")
            rec["actions"].append({"type": "draft_prepared", "content_id": content.content_id, "slot": p.slot, "publications": [x.id for x in pubs]})
            return True
        ok_platforms, blocked = [], []
        for v in ctx.content.current_versions(s, content.id):
            rep = ctx.content.latest_report(s, v.id)
            if rep is None or not rep.passed or rep.blocks_autopilot:
                blocked.append({"platform": v.platform, "reason": rep.summary if rep else "проверки не выполнены"})
            else:
                ok_platforms.append(v.platform)
        if not ok_platforms:
            rec["skipped"].append({"slot": p.slot, "title": ev.title[:60], "reasons": [b["reason"] for b in blocked][:3], "content_id": content.content_id})
            s.add(Notification(project_id=project_id, level="info", kind="autopilot_blocked", title=f"Автопилот не опубликовал «{ev.title[:60]}»", body="; ".join(b["reason"] for b in blocked)[:500] + ". Черновик ждёт вашего решения.", payload={"content_id": content.content_id}, created_at=now))
            return False
        pubs = ctx.publishing.create_for_content(s, project_id, content.id, actor, platforms=ok_platforms, schedule={"mode": "optimal"}, origin="autopilot")
        p.status = "selected"
        for pub in pubs:
            if pub.state != PubState.AWAITING_APPROVAL.value:
                continue
            at, meta = ctx.scheduling.resolve(s, project_id, pub.platform, {"mode": "optimal"}, now, account_id=pub.account_id, autonomous=True)
            pub.scheduled_at, pub.schedule, pub.approved_by, pub.approved_at = at, meta, actor.label, now
            ctx.publishing.transition(s, pub, PubState.SCHEDULED, actor, f"Автопилот: {meta.get('reason', '')}")
            rec["actions"].append({"type": "scheduled", "publication_id": pub.id, "platform": pub.platform, "at": at.isoformat(), "content_id": content.content_id, "trend_score": ev.trend_score})
        if blocked:
            rec["skipped"].append({"content_id": content.content_id, "blocked_platforms": blocked})
        return bool(rec["actions"])

    def _gate(self, s: Session, p: Proposal, ev: Event, cfg: Any, policy: AutopilotPolicy | None) -> list[str]:
        why: list[str] = []
        ap = cfg.autopilot
        if ev.trend_score < ap.min_trend_score:
            why.append(f"Trend Score {ev.trend_score:.0f} ниже порога автопилота {ap.min_trend_score:.0f}")
        if VERIF_RANK.get(ev.verification_status, 0) < VERIF_RANK.get(ap.min_verification, 2):
            why.append("проверка фактов ниже требуемого уровня автопилота")
        f = ev.flags or {}
        if f.get("political"):
            why.append("политическая тема — только ручное подтверждение")
        if f.get("sensitive") or f.get("sensitive_category"):
            why.append("чувствительная тема — только ручное подтверждение")
        if f.get("repeat_kind") == "duplicate":
            why.append("повтор уже опубликованной темы")
        if f.get("injection"):
            why.append("в источнике найдены инструкции для ИИ")
        allow = (policy.constraints or {}).get("categories") if policy else None
        block = (policy.constraints or {}).get("blocked_categories", []) if policy else []
        if allow and ev.category not in allow:
            why.append("категория не входит в разрешённые политикой")
        if ev.category in block:
            why.append("категория заблокирована политикой")
        return why
