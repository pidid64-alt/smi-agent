"""Воркер фоновых заданий с арендой (JobLease): несколько экземпляров не выполняют одно и то же задание одновременно (ТЗ §57–58)."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from ..db.models import AuthSession, HealthRecord, JobLease, LlmCall, LoginAttempt, Project, Report
from ..publishing.scheduling import tz_of
from ..security.rbac import Actor
from ..settings_model import load_project_settings

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval_s: int
    fn: Callable[[], Any]
    lease_ttl_s: int = 300


class Worker:
    def __init__(self, ctx: Any, *, holder: str | None = None):
        self.ctx = ctx
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}"
        self._stop = threading.Event()
        bk = max(60, ctx.settings.backup_interval_min * 60)
        self.jobs: list[Job] = [
            Job("publish", 20, self.j_publish, 120),
            Job("recover", 120, self.j_recover, 300),
            Job("monitor", 60, self.j_monitor, 900),
            Job("funnel", 120 * 60, self.j_funnel, 900),
            Job("autopilot", 300, self.j_autopilot, 900),
            Job("metrics", 1800, self.j_metrics, 1200),
            Job("backup", bk, self.j_backup, 900),
            Job("health", 60, self.j_health, 120),
            Job("reports", 3600, self.j_reports, 600),
            Job("audit_verify", 24 * 3600, self.j_audit_verify, 900),
            Job("cleanup", 24 * 3600, self.j_cleanup, 600),
        ]

    # ------------------------------------------------------------------- аренда
    def _due_and_acquire(self, job: Job) -> bool:
        now = self.ctx.clock.now()
        with self.ctx.db.session() as s:
            row = s.execute(select(JobLease).where(JobLease.name == job.name).with_for_update()).scalar_one_or_none()
            if row is None:
                row = JobLease(name=job.name, runs=0)
                s.add(row)
                s.flush()
            if row.lease_until and row.lease_until > now and row.holder != self.holder:
                return False
            if row.last_run_at and (now - row.last_run_at).total_seconds() < job.interval_s:
                return False
            row.holder, row.lease_until, row.last_run_at = self.holder, now + timedelta(seconds=job.lease_ttl_s), now
            return True

    def _finish(self, job: Job, ok: bool, error: str, started: float) -> None:
        with self.ctx.db.session() as s:
            row = s.get(JobLease, job.name)
            now = self.ctx.clock.now()
            row.lease_until, row.last_status, row.last_error, row.runs = None, "ok" if ok else "error", error[:1000], row.runs + 1
            row.last_duration_ms = int((time.monotonic() - started) * 1000)
            if ok:
                row.last_ok_at = now

    def tick(self, only: set[str] | None = None) -> dict[str, str]:
        """Выполняет все наступившие задания один раз. Возвращает имя → статус."""
        out: dict[str, str] = {}
        for job in self.jobs:
            if only and job.name not in only:
                continue
            try:
                if not self._due_and_acquire(job):
                    continue
            except Exception:  # noqa: BLE001
                log.exception("lease %s failed", job.name)
                continue
            started = time.monotonic()
            try:
                job.fn()
                self._finish(job, True, "", started)
                out[job.name] = "ok"
            except Exception as e:  # noqa: BLE001 — сбой одного задания не останавливает остальные
                log.exception("job %s failed", job.name)
                self._finish(job, False, f"{type(e).__name__}: {e}", started)
                out[job.name] = "error"
        return out

    def run_forever(self, *, poll_s: float = 5.0) -> None:
        def _sig(*_a: Any) -> None:
            self._stop.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _sig)
            signal.signal(signal.SIGINT, _sig)
        log.info("worker started holder=%s", self.holder)
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(poll_s)
        log.info("worker stopped")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- задания
    def _projects(self) -> list[int]:
        with self.ctx.db.read() as s:
            return [p.id for p in s.scalars(select(Project).where(Project.archived_at.is_(None)))]

    def j_publish(self) -> None:
        self.ctx.publishing.run_due()

    def j_recover(self) -> None:
        self.ctx.publishing.recover_stuck()

    def j_monitor(self) -> None:
        for pid in self._projects():
            self.ctx.ingest.poll_due(pid)
            self.ctx.events.process_new(pid)
            with self.ctx.db.session() as s:
                self.ctx.scoring.score_recent(s, pid)

    def j_funnel(self) -> None:
        for pid in self._projects():
            with self.ctx.db.session() as s:
                self.ctx.funnel.run(s, pid, Actor.system(), trigger="schedule")

    def j_autopilot(self) -> None:
        for pid in self._projects():
            self.ctx.autopilot.tick(pid)

    def j_metrics(self) -> None:
        for pid in self._projects():
            self.ctx.metrics.collect_due(pid)
            self.ctx.metrics.evaluate(pid)
            now = self.ctx.clock.now()
            if now.hour % 6 == 0:
                self.ctx.metrics.collect_audience(pid)

    def j_backup(self) -> None:
        res = self.ctx.backup.create()
        if datetime.fromisoformat(self.ctx.clock.now().isoformat()).minute < 15:  # примерно раз в час проверяем свежую копию
            self.ctx.backup.verify(res["path"], record_id=res["id"])

    def j_health(self) -> None:
        self.ctx.health.check()

    def j_reports(self) -> None:
        now = self.ctx.clock.now()
        for pid in self._projects():
            with self.ctx.db.session() as s:
                cfg = load_project_settings(s.get(Project, pid).settings)
                local = now.astimezone(tz_of(cfg))
                if local.weekday() == 0 and local.hour >= 8:
                    week_start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
                    if not s.scalars(select(Report).where(Report.project_id == pid, Report.kind == "weekly", Report.created_at >= week_start)).first():
                        self.ctx.analytics.build_report(s, pid, "weekly")
                if local.day <= 3 and local.hour >= 8:
                    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
                    if not s.scalars(select(Report).where(Report.project_id == pid, Report.kind == "monthly", Report.created_at >= month_start)).first():
                        self.ctx.analytics.build_report(s, pid, "monthly")

    def j_audit_verify(self) -> None:
        with self.ctx.db.read() as s:
            res = self.ctx.audit.verify_chain(s)
        with self.ctx.db.session() as s:
            s.add(HealthRecord(ts=self.ctx.clock.now(), component="audit_chain", status="ok" if res.ok else "fail", detail={"summary": f"проверено записей: {res.checked}" if res.ok else f"НАРУШЕНИЕ цепочки: запись {res.first_bad_id} — {res.reason}", "checked": res.checked}))

    def j_cleanup(self) -> None:
        now = self.ctx.clock.now()
        with self.ctx.db.session() as s:
            s.execute(delete(AuthSession).where((AuthSession.expires_at < now) | (AuthSession.revoked_at.is_not(None) & (AuthSession.revoked_at < now - timedelta(days=7)))))
            s.execute(delete(LoginAttempt).where(LoginAttempt.ts < now - timedelta(days=2)))
            s.execute(delete(HealthRecord).where(HealthRecord.ts < now - timedelta(days=30), HealthRecord.component != "audit_chain"))
            s.execute(delete(LlmCall).where(LlmCall.ts < now - timedelta(days=90)))
