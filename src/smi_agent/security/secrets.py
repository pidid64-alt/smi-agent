"""Защищённое хранилище секретов: AES-256-GCM, ключи с идентификаторами (ротация), аудит доступа, отзыв.

Мастер-ключи берутся из окружения (SMI_MASTER_KEYS) — в проде рекомендуется KMS/Vault, интерфейс не меняется.
В dev-режиме при отсутствии ключа создаётся локальный файл data/master.key (0600) — в Git не попадает.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets as pysecrets
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import AuditService
from ..config import Settings
from ..core.clock import Clock
from ..core.errors import AppError, NotFound
from ..db.models import Revocation, SecretRecord
from .rbac import Actor

log = logging.getLogger(__name__)


class SecretRevoked(AppError):
    status = 410
    code = "secret_revoked"


def generate_key_entry(key_id: str | None = None) -> str:
    kid = key_id or "k" + datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{kid}:{base64.urlsafe_b64encode(pysecrets.token_bytes(32)).decode()}"


def parse_master_keys(raw: str) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        kid, _, b64 = part.partition(":")
        key = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        if len(key) != 32:
            raise ValueError(f"Мастер-ключ {kid!r} должен быть 32 байта (base64url)")
        keys[kid] = key
    return keys


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


class SecretStore:
    def __init__(self, settings: Settings, audit: AuditService, clock: Clock):
        self.settings, self.audit, self.clock = settings, audit, clock
        raw = settings.master_keys.get_secret_value()
        if not raw:
            raw = self._dev_key_file()
        self.keys = parse_master_keys(raw)
        if not self.keys:
            raise RuntimeError("SMI_MASTER_KEYS не задан")
        self.current_id = next(iter(self.keys))

    def _dev_key_file(self) -> str:
        if self.settings.is_production:
            raise RuntimeError("В production обязателен SMI_MASTER_KEYS (команда: smi-agent keys generate)")
        path = Path(self.settings.data_dir) / "master.key"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(generate_key_entry("dev1"))
            log.warning("Создан dev-ключ шифрования %s (не для production)", path)
        return path.read_text().strip()

    # ---------------------------------------------------------------- crypto
    @staticmethod
    def _aad(project_id: int | None, name: str, key_id: str) -> bytes:
        return f"{project_id}:{name}:{key_id}".encode()

    def _encrypt(self, project_id: int | None, name: str, value: str) -> tuple[bytes, str]:
        nonce = pysecrets.token_bytes(12)
        ct = AESGCM(self.keys[self.current_id]).encrypt(nonce, value.encode(), self._aad(project_id, name, self.current_id))
        return nonce + ct, self.current_id

    def _decrypt(self, rec: SecretRecord) -> str:
        key = self.keys.get(rec.key_id)
        if key is None:
            raise AppError("Ключ шифрования недоступен", code="secret_key_missing")
        blob = rec.ciphertext
        return AESGCM(key).decrypt(blob[:12], blob[12:], self._aad(rec.project_id, rec.name, rec.key_id)).decode()

    # ------------------------------------------------------------------- API
    def put(self, s: Session, actor: Actor, project_id: int | None, name: str, value: str, *, kind: str = "token") -> SecretRecord:
        rec = s.execute(select(SecretRecord).where(SecretRecord.project_id == project_id, SecretRecord.name == name)).scalar_one_or_none()
        ct, kid = self._encrypt(project_id, name, value)
        now = self.clock.now()
        if rec is None:
            rec = SecretRecord(project_id=project_id, name=name, kind=kind, ciphertext=ct, key_id=kid, created_by=actor.label)
            s.add(rec)
            action = "secret.create"
        else:
            rec.ciphertext, rec.key_id, rec.version = ct, kid, rec.version + 1
            rec.rotated_at, rec.revoked_at, rec.revoked_by = now, None, None
            action = "secret.rotate"
        s.flush()
        self.audit.log(s, actor, action, project_id=project_id, target_type="secret", target_id=rec.id, details={"name": name, "kind": kind, "version": rec.version})
        return rec

    def get(self, s: Session, actor: Actor, secret_id: int, *, purpose: str = "") -> str:
        rec = s.get(SecretRecord, secret_id)
        if rec is None:
            raise NotFound("Секрет не найден")
        if rec.revoked_at is not None:
            raise SecretRevoked("Секрет отозван и не может использоваться")
        value = self._decrypt(rec)
        rec.last_access_at = self.clock.now()
        self.audit.log(s, actor, "secret.access", project_id=rec.project_id, target_type="secret", target_id=rec.id, details={"name": rec.name, "purpose": purpose})
        return value

    def revoke(self, s: Session, actor: Actor, secret_id: int, *, reason: str = "") -> None:
        rec = s.get(SecretRecord, secret_id)
        if rec is None:
            raise NotFound("Секрет не найден")
        if rec.revoked_at is None:
            rec.revoked_at, rec.revoked_by = self.clock.now(), actor.label
            rec.ciphertext = b""  # инвалидация: шифртекст уничтожается
            s.add(Revocation(ts=rec.revoked_at, project_id=rec.project_id, kind="secret", ref=str(rec.id), reason=reason[:300]))
            s.flush()
            self.append_ledger("secret", str(rec.id), reason, rec.project_id)
        self.audit.log(s, actor, "secret.revoke", project_id=rec.project_id, target_type="secret", target_id=rec.id, details={"name": rec.name, "reason": reason})

    def rotate_master_key(self, s: Session, actor: Actor) -> int:
        """Перешифровывает все живые секреты текущим ключом (после добавления нового ключа первым в SMI_MASTER_KEYS)."""
        n = 0
        for rec in s.execute(select(SecretRecord).where(SecretRecord.revoked_at.is_(None))).scalars():
            if rec.key_id != self.current_id:
                value = self._decrypt(rec)
                rec.ciphertext, rec.key_id = self._encrypt(rec.project_id, rec.name, value)
                rec.rotated_at = self.clock.now()
                n += 1
        self.audit.log(s, actor, "secret.master_rotate", details={"reencrypted": n, "key_id": self.current_id})
        return n

    # --------------------------------------------------- ledger (для восстановления)
    def ledger_path(self) -> Path:
        return Path(self.settings.data_dir) / "revocations.jsonl"

    def append_ledger(self, kind: str, ref: str, reason: str, project_id: int | None) -> None:
        path = self.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": self.clock.now().isoformat(), "kind": kind, "ref": ref, "reason": reason[:300], "project_id": project_id}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
