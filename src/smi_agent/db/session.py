"""Подключение к БД: SQLite (dev/одиночный узел) и PostgreSQL (прод) через SQLAlchemy.

SQLite: WAL, foreign_keys, busy_timeout; сессии записи стартуют с BEGIN IMMEDIATE — это сериализует писателей
и исключает «устаревший снимок» при цепочке аудита и захвате заданий публикации.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .base import Base
from .models import AuditHead, SchemaVersion

SCHEMA_VERSION = 1

_SQLITE_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
]
_PG_TRIGGERS = [
    """CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log",
    """CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
       FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()""",
]


class Database:
    def __init__(self, url: str, *, echo: bool = False):
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        self.is_memory = self.is_sqlite and (url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url)
        kwargs: dict = {"echo": echo, "future": True}
        if self.is_sqlite:
            if not self.is_memory:
                path = url.replace("sqlite:///", "", 1)
                if path:
                    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if self.is_memory:
                kwargs["poolclass"] = StaticPool
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: Engine = create_engine(url, **kwargs)
        if self.is_sqlite:
            self._wire_sqlite(self.engine)
        self._write_engine = self.engine.execution_options(sqlite_write=True) if self.is_sqlite else self.engine
        self._local = threading.local()
        self._read_factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=True)
        self._write_factory = sessionmaker(bind=self._write_engine, expire_on_commit=False, autoflush=True)

    @staticmethod
    def _wire_sqlite(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.isolation_level = None  # транзакциями управляем сами (см. _on_begin)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):  # noqa: ANN001
            immediate = conn.get_execution_options().get("sqlite_write", False)
            conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    # ------------------------------------------------------------------ sessions
    def in_write(self) -> bool:
        """True, если текущий поток держит открытую транзакцию записи (недопустимо делать сетевые вызовы/LLM)."""
        return getattr(self._local, "write_depth", 0) > 0

    @contextmanager
    def session(self, *, write: bool = True) -> Iterator[Session]:
        factory = self._write_factory if write else self._read_factory
        s = factory()
        if write:
            self._local.write_depth = getattr(self._local, "write_depth", 0) + 1
        try:
            yield s
            if write:
                s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()
            if write:
                self._local.write_depth -= 1

    def read(self):
        return self.session(write=False)

    # -------------------------------------------------------------------- schema
    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._install_triggers()
        with self.session() as s:
            if s.get(AuditHead, 1) is None:
                s.add(AuditHead(id=1, last_id=0, last_hash="0" * 64))
            if s.query(SchemaVersion).first() is None:
                s.add(SchemaVersion(version=SCHEMA_VERSION))

    def _install_triggers(self) -> None:
        stmts = _SQLITE_TRIGGERS if self.is_sqlite else _PG_TRIGGERS if self.engine.dialect.name == "postgresql" else []
        with self.engine.begin() as conn:
            for st in stmts:
                conn.execute(text(st))

    def dispose(self) -> None:
        self.engine.dispose()

    def file_path(self) -> Path | None:
        if self.is_sqlite and not self.is_memory:
            return Path(self.url.replace("sqlite:///", "", 1)).expanduser().resolve()
        return None


def default_db_url(data_dir: str | os.PathLike) -> str:
    return f"sqlite:///{Path(data_dir).resolve() / 'smi_agent.db'}"
