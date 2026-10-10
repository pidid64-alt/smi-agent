"""Идентификаторы публикуемых материалов: Content-YYYY-NNNNNN (ТЗ §24)."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ContentSequence

CONTENT_ID_RE = re.compile(r"^Content-(\d{4})-(\d{6})$")


def next_content_id(s: Session, now: datetime) -> str:
    """Атомарно выдаёт следующий номер. Вызывать внутри транзакции записи (SQLite: BEGIN IMMEDIATE; PG: FOR UPDATE)."""
    year = now.year
    row = s.execute(select(ContentSequence).where(ContentSequence.year == year).with_for_update()).scalar_one_or_none()
    if row is None:
        row = ContentSequence(year=year, last_value=0)
        s.add(row)
        s.flush()
    row.last_value += 1
    s.flush()
    return f"Content-{year}-{row.last_value:06d}"
