"""Мониторинг состояния (ТЗ §57): компоненты, пороги, история, оповещения при ухудшении."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from ..core.enums import AccountStatus, PubState
from ..db.models import (
    BackupRecord,
    HealthRecord,
    JobLease,
    KillSwitch,
    LlmCall,
    Notification,
    PlatformAccount,
    Project,
    Publication,
)
from ..publishing.service import STUCK_AFTER

RANK = {"ok": 0, "warn": 1, "fail": 2}
# ожидаемые задания воркера и допустимая «тишина» (с) до предупреждения
EXPECTED_JOBS = {"publish": 120, "monitor": 900, "funnel": 4 * 3600, "autopilot": 1200, "recover": 600, "metrics": 7200, "backup": 3600, "health": 300}


class HealthService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def _c(self, status: str, summary: str, **detail: Any) -> dict[str, Any]:
        return {"status": status, "summary": summary, "detail": detail}

    def check(self, *, record: bool = True) -> dict[str, Any]:
        ctx = self.ctx
        now = ctx.clock.now()
        comps: dict[str, dict[str, Any]] = {}
        with ctx.db.read() as s:
            # --- БД
            try:
                s.execute(text("SELECT 1"))
                size = ctx.db.file_path().stat().st_size if ctx.db.file_path() and ctx.db.file_path().exists() else None
                comps["database"] = self._c("ok", "БД доступна", size_bytes=size, engine=ctx.db.engine.dialect.name)
            except Exception as e:  # noqa: BLE001
                comps["database"] = self._c("fail", f"БД недоступна: {type(e).__name__}")
            # --- источники
            projects = list(s.scalars(select(Project).where(Project.archived_at.is_(None))))
            bad, total, mandatory_bad = 0, 0, []
            for p in projects:
                for h in ctx.ingest.source_health(s, p.id):
                    if h["state"] in ("disabled", "manual"):
                        continue
                    total += 1
                    if h["state"] in ("failing", "stale"):
                        bad += 1
                        if h["mandatory"]:
                            mandatory_bad.append(h["name"])
            if mandatory_bad:
                comps["sources"] = self._c("fail", f"Обязательный источник не работает: {', '.join(mandatory_bad)}", failing=bad, total=total)
            elif total and bad / total >= 0.3:
                comps["sources"] = self._c("warn", f"Проблемы с источниками: {bad} из {total}", failing=bad, total=total)
            else:
                comps["sources"] = self._c("ok", f"Источники: проблем {bad} из {total}", failing=bad, total=total)
            # --- воркер
            leases = {j.name: j for j in s.scalars(select(JobLease))}
            stale = []
            for name, tol in EXPECTED_JOBS.items():
                j = leases.get(name)
                if j is None or j.last_ok_at is None or (now - j.last_ok_at).total_seconds() > tol:
                    stale.append(name)
            failing_jobs = [n for n, j in leases.items() if j.last_status == "error"]
            if not leases:
                comps["worker"] = self._c("warn", "Воркер ещё не запускался (фоновые задачи не выполняются)")
            elif "publish" in stale:
                comps["worker"] = self._c("fail", "Задание публикации не выполнялось вовремя", stale=stale)
            elif stale or failing_jobs:
                comps["worker"] = self._c("warn", f"Отстают задания: {', '.join(stale + failing_jobs)}", stale=stale, failing=failing_jobs)
            else:
                comps["worker"] = self._c("ok", "Фоновые задания выполняются", jobs=len(leases))
            # --- очередь публикаций
            overdue = s.scalar(select(func.count()).select_from(Publication).where(Publication.state == PubState.SCHEDULED.value, Publication.scheduled_at < now - timedelta(minutes=10), Publication.next_attempt_at.is_(None))) or 0
            stuck = s.scalar(select(func.count()).select_from(Publication).where(Publication.state == PubState.PUBLISHING.value, Publication.claimed_at < now - STUCK_AFTER)) or 0
            manual = s.scalar(select(func.count()).select_from(Publication).where(Publication.needs_manual.is_(True), Publication.state == PubState.ERROR.value)) or 0
            errors24 = s.scalar(select(func.count()).select_from(Publication).where(Publication.state == PubState.ERROR.value, Publication.updated_at >= now - timedelta(hours=24))) or 0
            if stuck or overdue:
                comps["publishing"] = self._c("fail" if overdue else "warn", f"Просрочено: {overdue}; зависло: {stuck}", overdue=overdue, stuck=stuck, needs_manual=manual)
            elif manual or errors24:
                comps["publishing"] = self._c("warn", f"Требуют внимания: ручная проверка {manual}, ошибки за сутки {errors24}", needs_manual=manual, errors_24h=errors24)
            else:
                comps["publishing"] = self._c("ok", "Очередь публикаций в порядке")
            # --- аккаунты
            accs = list(s.scalars(select(PlatformAccount)))
            need = [a for a in accs if a.status in (AccountStatus.NEEDS_REAUTH.value, AccountStatus.ERROR.value)]
            expiring = [a for a in accs if a.token_expires_at and a.token_expires_at < now + timedelta(days=7) and a.status == AccountStatus.CONNECTED.value]
            if need:
                comps["accounts"] = self._c("warn", f"Аккаунтов требуют внимания: {len(need)}", accounts=[f"{a.platform}:{a.display_name}" for a in need])
            elif expiring:
                comps["accounts"] = self._c("warn", f"Токены истекают в течение 7 дней: {len(expiring)}")
            else:
                comps["accounts"] = self._c("ok", f"Аккаунтов подключено: {sum(1 for a in accs if a.status == 'connected')}")
            # --- LLM
            if not ctx.llm.enabled:
                comps["llm"] = self._c("ok", "LLM не подключена — используются эвристики (автопубликация текстов запрещена)", enabled=False)
            else:
                since = now - timedelta(hours=24)
                total_calls = s.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.ts >= since)) or 0
                failed = s.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.ts >= since, LlmCall.ok.is_(False))) or 0
                rate = failed / total_calls if total_calls else 0
                comps["llm"] = self._c("warn" if total_calls >= 5 and rate > 0.5 else "ok", f"Вызовов за сутки: {total_calls}, неудачных: {failed}", enabled=True, calls=total_calls, failed=failed)
            # --- резервные копии
            last = s.scalars(select(BackupRecord).order_by(BackupRecord.id.desc()).limit(1)).first()
            interval = timedelta(minutes=ctx.settings.backup_interval_min)
            if last is None:
                comps["backup"] = self._c("fail" if ctx.settings.is_production else "warn", "Резервных копий нет")
            elif last.verify_status == "failed":
                comps["backup"] = self._c("fail", "Последняя копия не прошла проверку")
            elif now - last.ts > 3 * interval:
                comps["backup"] = self._c("warn", f"Последняя копия старше {int(3 * interval.total_seconds() // 60)} мин (RPO под угрозой)", last=last.ts.isoformat())
            else:
                comps["backup"] = self._c("ok", f"Последняя копия {int((now - last.ts).total_seconds() // 60)} мин назад", last=last.ts.isoformat(), verified=last.verify_status or "не проверялась")
            # --- диск
            try:
                du = shutil.disk_usage(Path(ctx.settings.data_dir) if Path(ctx.settings.data_dir).exists() else ".")
                free = du.free / du.total
                comps["disk"] = self._c("fail" if free < 0.03 else "warn" if free < 0.10 else "ok", f"Свободно {free:.0%}", free_gb=round(du.free / 1e9, 1))
            except OSError:
                comps["disk"] = self._c("warn", "Не удалось определить свободное место")
            # --- аудит (результат ночной проверки цепочки)
            chain = s.scalars(select(HealthRecord).where(HealthRecord.component == "audit_chain").order_by(HealthRecord.id.desc()).limit(1)).first()
            comps["audit_chain"] = self._c(chain.status, chain.detail.get("summary", "")) if chain else self._c("ok", "Проверка цепочки ещё не выполнялась")
            # --- аварийные выключатели
            ks = list(s.scalars(select(KillSwitch).where(KillSwitch.released_at.is_(None))))
            comps["killswitch"] = self._c("warn" if ks else "ok", f"Автопилот остановлен: {len(ks)} выключател(ь/я)" if ks else "Аварийных остановок нет", active=[f"{k.scope_type}:{k.scope_value}" for k in ks])
            comps["config"] = self._config_lint()
        overall = max((c["status"] for c in comps.values()), key=lambda x: RANK[x])
        result = {"status": overall, "checked_at": now.isoformat(), "components": comps}
        if record:
            self._record(result)
        return result

    def _config_lint(self) -> dict[str, Any]:
        st = self.ctx.settings
        issues = []
        if st.is_production:
            if not st.master_keys.get_secret_value():
                issues.append("не задан SMI_MASTER_KEYS")
            if not st.backup_key.get_secret_value():
                issues.append("не задан SMI_BACKUP_KEY")
            if not st.public_url.startswith("https://"):
                issues.append("public_url должен быть https (TLS)")
            if st.allow_private_fetch:
                issues.append("включён allow_private_fetch")
            if st.demo_mode:
                issues.append("включён демо-режим")
            if st.docs_enabled:
                issues.append("открыта документация API (/docs)")
            if not st.mfa_required_for_admin:
                issues.append("MFA для администратора отключён")
        return self._c("warn" if issues else "ok", "Проблемы конфигурации: " + "; ".join(issues) if issues else "Конфигурация в порядке", issues=issues, env=st.env)

    def _record(self, result: dict[str, Any]) -> None:
        ctx = self.ctx
        now = ctx.clock.now()
        with ctx.db.session() as s:
            for name, c in result["components"].items():
                prev = s.scalars(select(HealthRecord).where(HealthRecord.component == name).order_by(HealthRecord.id.desc()).limit(1)).first()
                changed = prev is None or prev.status != c["status"]
                if changed or (now - prev.ts) > timedelta(minutes=30):
                    s.add(HealthRecord(ts=now, component=name, status=c["status"], detail={"summary": c["summary"], **c["detail"]}))
                if changed and prev is not None and RANK[c["status"]] > RANK[prev.status]:
                    recent = s.scalars(select(Notification).where(Notification.kind == f"health:{name}", Notification.created_at >= now - timedelta(hours=1))).first()
                    if recent is None:
                        s.add(Notification(project_id=None, level="critical" if c["status"] == "fail" else "warning", kind=f"health:{name}", title=f"Состояние «{name}»: {c['status']}", body=c["summary"], payload={"component": name}, created_at=now))

    def history(self, component: str | None = None, hours: int = 24) -> list[dict[str, Any]]:
        since = self.ctx.clock.now() - timedelta(hours=hours)
        with self.ctx.db.read() as s:
            q = select(HealthRecord).where(HealthRecord.ts >= since).order_by(HealthRecord.id.desc()).limit(500)
            if component:
                q = q.where(HealthRecord.component == component)
            return [{"ts": h.ts.isoformat(), "component": h.component, "status": h.status, "detail": h.detail} for h in s.scalars(q)]

    def prometheus(self, result: dict[str, Any] | None = None) -> str:
        r = result or self.check(record=False)
        lines = ["# HELP smi_component_status 0=ok 1=warn 2=fail", "# TYPE smi_component_status gauge"]
        for n, c in r["components"].items():
            lines.append(f'smi_component_status{{component="{n}"}} {RANK[c["status"]]}')
        lines.append(f"smi_overall_status {RANK[r['status']]}")
        return "\n".join(lines) + "\n"
