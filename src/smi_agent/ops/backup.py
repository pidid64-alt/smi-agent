"""Резервное копирование и восстановление (ТЗ §57–58): консистентный снимок SQLite, gzip + AES-256-GCM, проверка восстановимости.

RPO = интервал копирования (по умолчанию 15 мин). RTO измеряется командой `smi-agent backup restore-test` (см. docs/OPERATIONS.md).
Отзывы доступа (журнал revocations.jsonl) применяются заново после восстановления: отозванные секреты не «воскресают» из копии.
Для PostgreSQL используется pg_dump (если установлен); рекомендуемая схема для прода — WAL-архивация/pgBackRest (не проверялось в этой среде).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import secrets as pysecrets
import shutil
import sqlite3
import subprocess  # noqa: S404 — pg_dump
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, update

from ..core.errors import AppError
from ..db.models import BackupRecord, PlatformAccount, Revocation, SecretRecord

log = logging.getLogger(__name__)
MAGIC = b"SMIBK1"


class BackupError(AppError):
    status = 500
    code = "backup_error"


def _ts_name(dt) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


class BackupService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------ ключ
    def _key(self) -> bytes:
        st = self.ctx.settings
        raw = st.backup_key.get_secret_value()
        if not raw:
            if st.is_production:
                raise BackupError("В production обязателен отдельный SMI_BACKUP_KEY (команда: smi-agent keys generate)")
            path = Path(st.data_dir) / "backup.key"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # 0600 действует на POSIX; на Windows права наследуются от каталога
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(base64.urlsafe_b64encode(pysecrets.token_bytes(32)).decode())
                log.warning("Создан dev-ключ резервных копий %s (не для production)", path)
            raw = path.read_text(encoding="utf-8-sig").strip()
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(key) != 32:
            raise BackupError("SMI_BACKUP_KEY должен быть 32 байта в base64url")
        return key

    def _encrypt(self, data: bytes) -> bytes:
        nonce = pysecrets.token_bytes(12)
        return MAGIC + nonce + AESGCM(self._key()).encrypt(nonce, data, MAGIC)

    def _decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(MAGIC):
            raise BackupError("Неизвестный формат резервной копии")
        nonce, ct = blob[len(MAGIC) : len(MAGIC) + 12], blob[len(MAGIC) + 12 :]
        try:
            return AESGCM(self._key()).decrypt(nonce, ct, MAGIC)
        except InvalidTag as e:
            raise BackupError("Копия повреждена или изменена (проверка целостности не пройдена) либо неверный ключ") from e

    # ---------------------------------------------------------------- создание
    def _dump(self) -> bytes:
        db = self.ctx.db
        if db.is_sqlite and not db.is_memory:
            src_path = db.file_path()
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "snap.db"
                src = sqlite3.connect(str(src_path), timeout=30)
                dst = sqlite3.connect(str(tmp))
                try:
                    src.backup(dst)  # онлайн-копия: консистентна при параллельной записи (WAL)
                finally:
                    dst.close()
                    src.close()
                return tmp.read_bytes()
        if db.engine.dialect.name == "postgresql":
            if shutil.which("pg_dump") is None:
                raise BackupError("pg_dump не найден. Для PostgreSQL установите клиент PostgreSQL либо используйте WAL-архивацию/pgBackRest")
            u = urlsplit(self.ctx.settings.database_url.replace("+psycopg", ""))
            env = {**os.environ, "PGPASSWORD": unquote(u.password or "")}
            cmd = ["pg_dump", "--format=custom", "-h", u.hostname or "localhost", "-p", str(u.port or 5432), "-U", unquote(u.username or ""), (u.path or "/").lstrip("/")]
            r = subprocess.run(cmd, capture_output=True, env=env, timeout=600, check=False)  # noqa: S603
            if r.returncode != 0:
                raise BackupError("pg_dump завершился с ошибкой")
            return r.stdout
        raise BackupError("Резервное копирование поддерживается для файловой SQLite и PostgreSQL")

    def create(self, *, note: str = "") -> dict[str, Any]:
        ctx = self.ctx
        now = ctx.clock.now()
        t0 = time.monotonic()
        raw = self._dump()
        blob = self._encrypt(gzip.compress(raw, compresslevel=6))
        bdir = Path(ctx.settings.backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        ext = "db" if ctx.db.is_sqlite else "pgdump"
        path = bdir / f"smi-{_ts_name(now)}.{ext}.gz.enc"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        ledger = Path(ctx.settings.data_dir) / "revocations.jsonl"
        if ledger.exists():
            shutil.copy2(ledger, bdir / "revocations.jsonl")  # журнал отзывов хранится рядом с копиями
        digest = hashlib.sha256(blob).hexdigest()
        with ctx.db.session() as s:
            rec = BackupRecord(ts=now, kind="db", path=str(path), size_bytes=len(blob), sha256=digest, encrypted=True, note=note[:300])
            s.add(rec)
            s.flush()
            rid = rec.id
        self.prune()
        return {"id": rid, "path": str(path), "size_bytes": len(blob), "sha256": digest, "seconds": round(time.monotonic() - t0, 2)}

    # -------------------------------------------------------------- проверка / восстановление
    def _open(self, path: str | Path) -> bytes:
        blob = Path(path).read_bytes()
        return gzip.decompress(self._decrypt(blob))

    def verify(self, path: str | Path, *, record_id: int | None = None) -> dict[str, Any]:
        """Расшифровка → проверка целостности SQLite → проверка хеш-цепочки аудита. Результат пишется в журнал копий."""
        res: dict[str, Any] = {"ok": False, "path": str(path)}
        try:
            data = self._open(path)
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "v.db"
                tmp.write_bytes(data)
                con = sqlite3.connect(str(tmp))
                try:
                    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                    tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                finally:
                    con.close()
                from ..audit import AuditService
                from ..db import Database

                vdb = Database(f"sqlite:///{tmp}")
                try:
                    with vdb.read() as s:
                        chain = AuditService(self.ctx.clock).verify_chain(s)
                finally:
                    vdb.dispose()
            res.update(ok=integrity == "ok" and chain.ok, integrity=integrity, tables=tables, audit_chain_ok=chain.ok, audit_records=chain.checked)
        except (BackupError, OSError, sqlite3.DatabaseError, gzip.BadGzipFile) as e:
            res["error"] = getattr(e, "message", str(e))
        if record_id is not None:
            with self.ctx.db.session() as s:
                rec = s.get(BackupRecord, record_id)
                if rec is not None:
                    rec.verified_at, rec.verify_status = self.ctx.clock.now(), "ok" if res["ok"] else "failed"
        return res

    def restore(self, path: str | Path, target: str | Path, *, force: bool = False, apply_ledger: bool = True) -> dict[str, Any]:
        t0 = time.monotonic()
        target = Path(target)
        if target.exists() and not force:
            raise BackupError(f"Файл {target} существует; укажите --force (старый будет сохранён как .bak)")
        data = self._open(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
        for ext in ("-wal", "-shm"):
            Path(str(target) + ext).unlink(missing_ok=True)
        target.write_bytes(data)
        revoked = self.replay_revocations(target) if apply_ledger else 0
        return {"target": str(target), "seconds": round(time.monotonic() - t0, 2), "revocations_replayed": revoked}

    def replay_revocations(self, db_path: str | Path) -> int:
        """Применяет журнал отзывов к восстановленной БД: секрет, отозванный после копии, остаётся отозванным."""
        from ..db import Database

        entries: dict[tuple[str, str], dict[str, Any]] = {}
        for ledger in (Path(self.ctx.settings.data_dir) / "revocations.jsonl", Path(self.ctx.settings.backup_dir) / "revocations.jsonl"):
            if ledger.exists():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    try:
                        e = json.loads(line)
                        entries[(e["kind"], str(e["ref"]))] = e
                    except (ValueError, KeyError):
                        continue
        if not entries:
            return 0
        db = Database(f"sqlite:///{db_path}")
        n = 0
        try:
            with db.session() as s:
                for (kind, ref), e in entries.items():
                    if kind != "secret" or not ref.isdigit():
                        continue
                    sec = s.get(SecretRecord, int(ref))
                    if sec is not None and sec.revoked_at is None:
                        sec.ciphertext, sec.revoked_at, sec.revoked_by = b"", self.ctx.clock.now(), "ledger-replay"
                        s.add(Revocation(ts=self.ctx.clock.now(), project_id=sec.project_id, kind="secret", ref=ref, reason=f"replayed: {e.get('reason', '')}"[:300]))
                        n += 1
                    s.execute(update(PlatformAccount).where(PlatformAccount.secret_id == int(ref)).values(status="revoked", last_error="отзыв восстановлен из журнала"))
        finally:
            db.dispose()
        return n

    def restore_test(self, path: str | Path | None = None) -> dict[str, Any]:
        """Проверка восстановления «как на учениях»: копия → временный файл → целостность → замер времени (RTO)."""
        if path is None:
            with self.ctx.db.read() as s:
                rec = s.scalars(select(BackupRecord).order_by(BackupRecord.id.desc()).limit(1)).first()
            if rec is None:
                raise BackupError("Нет ни одной резервной копии")
            path, rid = rec.path, rec.id
        else:
            rid = None
        with tempfile.TemporaryDirectory() as td:
            t0 = time.monotonic()
            out = self.restore(path, Path(td) / "restored.db", force=True)
            v = self.verify(path, record_id=rid)
            total = round(time.monotonic() - t0, 2)
        if rid:
            with self.ctx.db.session() as s:
                r = s.get(BackupRecord, rid)
                r.note = f"restore-test: {total} с; целостность: {v.get('integrity')}; цепочка аудита: {v.get('audit_chain_ok')}"
        return {"ok": v["ok"], "rto_seconds": total, "verify": v, "restore": out}

    # ----------------------------------------------------------------------- учёт
    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            return [{"id": r.id, "ts": r.ts.isoformat(), "path": r.path, "size_bytes": r.size_bytes, "sha256": r.sha256, "encrypted": r.encrypted, "verify_status": r.verify_status, "verified_at": r.verified_at.isoformat() if r.verified_at else None, "note": r.note} for r in s.scalars(select(BackupRecord).order_by(BackupRecord.id.desc()).limit(limit))]

    def prune(self) -> int:
        keep = self.ctx.settings.backup_retention
        removed = 0
        with self.ctx.db.session() as s:
            rows = list(s.scalars(select(BackupRecord).order_by(BackupRecord.id.desc())))
            last_ok = next((r for r in rows if r.verify_status == "ok"), None)
            for r in rows[keep:]:
                if last_ok is not None and r.id == last_ok.id:
                    continue  # последнюю проверенную копию не удаляем
                Path(r.path).unlink(missing_ok=True)
                s.delete(r)
                removed += 1
        return removed
