"""Журнал аудита с хеш-цепочкой (tamper-evident) и append-only триггерами в БД (ТЗ §36, §56)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.clock import Clock
from ..db.models import AuditHead, AuditLog
from ..security.rbac import Actor
from ..security.redaction import sanitize_details

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_hash(prev_hash: str, row_id: int, payload: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash}|{row_id}|{_canonical(payload)}".encode()).hexdigest()


def _payload(row: AuditLog) -> dict[str, Any]:
    return {
        "ts": row.ts.astimezone(UTC).isoformat(timespec="microseconds"),
        "project_id": row.project_id,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "actor_role": row.actor_role,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "outcome": row.outcome,
        "details": row.details,
        "ip": row.ip,
    }


@dataclass
class VerifyResult:
    ok: bool
    checked: int
    first_bad_id: int | None = None
    reason: str = ""


class AuditService:
    def __init__(self, clock: Clock):
        self.clock = clock

    def log(
        self,
        s: Session,
        actor: Actor,
        action: str,
        *,
        project_id: int | None = None,
        target_type: str = "",
        target_id: str | int = "",
        outcome: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        # Захватываем «голову» цепочки под блокировкой строки (PG: FOR UPDATE; SQLite: BEGIN IMMEDIATE).
        head = s.execute(select(AuditHead).where(AuditHead.id == 1).with_for_update()).scalar_one()
        row = AuditLog(
            id=head.last_id + 1,
            ts=self.clock.now(),
            project_id=project_id if project_id is not None else actor.project_id,
            actor_type=actor.type,
            actor_id=actor.id,
            actor_role=actor.role,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            outcome=outcome,
            details=sanitize_details(details or {}),
            ip=actor.ip,
            prev_hash=head.last_hash,
            hash="",
        )
        row.hash = compute_hash(head.last_hash, row.id, _payload(row))
        s.add(row)
        head.last_id = row.id
        head.last_hash = row.hash
        s.flush()
        return row

    def verify_chain(self, s: Session, *, batch: int = 1000) -> VerifyResult:
        prev = GENESIS
        expected_id = 1
        checked = 0
        last_id = 0
        q = select(AuditLog).order_by(AuditLog.id)
        for row in s.execute(q.execution_options(yield_per=batch)).scalars():
            if row.id != expected_id:
                return VerifyResult(False, checked, row.id, "пропуск записи в журнале (удаление?)")
            if row.prev_hash != prev:
                return VerifyResult(False, checked, row.id, "нарушена связь хеш-цепочки")
            if compute_hash(row.prev_hash, row.id, _payload(row)) != row.hash:
                return VerifyResult(False, checked, row.id, "содержимое записи изменено")
            prev = row.hash
            expected_id += 1
            checked += 1
            last_id = row.id
        head = s.get(AuditHead, 1)
        if head is None or head.last_id != last_id or head.last_hash != prev:
            return VerifyResult(False, checked, last_id + 1, "голова цепочки не совпадает (усечение хвоста?)")
        return VerifyResult(True, checked)

    def query(
        self,
        s: Session,
        *,
        project_id: int | None = None,
        action: str | None = None,
        actor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AuditLog]:
        q = select(AuditLog).order_by(AuditLog.id.desc())
        if project_id is not None:
            q = q.where((AuditLog.project_id == project_id) | (AuditLog.project_id.is_(None)))
        if action:
            q = q.where(AuditLog.action.like(f"{action}%"))
        if actor:
            q = q.where(AuditLog.actor_id == actor)
        if since:
            q = q.where(AuditLog.ts >= since)
        if until:
            q = q.where(AuditLog.ts <= until)
        return list(s.execute(q.limit(limit).offset(offset)).scalars())

    def export(self, s: Session, *, project_id: int | None = None) -> Iterator[dict[str, Any]]:
        q = select(AuditLog).order_by(AuditLog.id)
        if project_id is not None:
            q = q.where((AuditLog.project_id == project_id) | (AuditLog.project_id.is_(None)))
        for row in s.execute(q.execution_options(yield_per=500)).scalars():
            yield {**_payload(row), "id": row.id, "prev_hash": row.prev_hash, "hash": row.hash}


def row_to_dict(row: AuditLog) -> dict[str, Any]:
    return {**_payload(row), "id": row.id, "hash": row.hash}
