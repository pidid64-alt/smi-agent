"""ORM-модели: база знаний агента (ТЗ §50) + безопасность, публикации, аналитика, аудит.

Все сущности предметной области привязаны к project_id (изоляция проектов, ТЗ §36).
Временные метки — только UTC (см. UTCDateTime).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UTCDateTime, utcnow


def _fk(table: str, *, nullable: bool = False, ondelete: str = "CASCADE", index: bool = True):
    return mapped_column(Integer, ForeignKey(f"{table}.id", ondelete=ondelete), nullable=nullable, index=index)


def _js(default=dict):
    return mapped_column(JSON, default=default)


def _ts(**kw):
    return mapped_column(UTCDateTime, **kw)


# ======================================================================= доступ ===
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    settings: Mapped[dict[str, Any]] = _js()
    created_at: Mapped[datetime] = _ts(default=utcnow)
    archived_at: Mapped[datetime | None] = _ts(nullable=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(16), default="human")  # human | service
    password_hash: Mapped[str] = mapped_column(String(300), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # SecretRecord.id
    mfa_last_step: Mapped[int] = mapped_column(Integer, default=0)  # защита TOTP от повторного использования
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    last_login_at: Mapped[datetime | None] = _ts(nullable=True)
    password_changed_at: Mapped[datetime | None] = _ts(nullable=True)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = _fk("users")
    project_id: Mapped[int] = _fk("projects")
    role: Mapped[str] = mapped_column(String(16))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = _fk("users")
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts(default=utcnow)
    last_seen_at: Mapped[datetime] = _ts(default=utcnow)
    expires_at: Mapped[datetime] = _ts()
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    user_id: Mapped[int] = _fk("users")  # сервисный пользователь
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    last_used_at: Mapped[datetime | None] = _ts(nullable=True)
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    ts: Mapped[datetime] = _ts(default=utcnow, index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)


class SecretRecord(Base):
    """Зашифрованное хранилище токенов/ключей (AES-256-GCM, AAD=project:name:key_id)."""

    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40), default="token")
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    key_id: Mapped[str] = mapped_column(String(40), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="")
    rotated_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_access_at: Mapped[datetime | None] = _ts(nullable=True)


class Revocation(Base):
    """Журнал отзывов (дублируется в revocations.jsonl рядом с бэкапами — не воскрешать отозванное при восстановлении)."""

    __tablename__ = "revocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts(default=utcnow)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # secret | account | token | session
    ref: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(300), default="")


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_project_ts", "project_id", "ts"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts(default=utcnow, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(16))  # user | service | system
    actor_id: Mapped[str] = mapped_column(String(80), default="")
    actor_role: Mapped[str] = mapped_column(String(16), default="")
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(80), default="")
    outcome: Mapped[str] = mapped_column(String(12), default="ok")
    details: Mapped[dict[str, Any]] = _js()
    ip: Mapped[str] = mapped_column(String(64), default="")
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class AuditHead(Base):
    __tablename__ = "audit_head"
    id: Mapped[int] = mapped_column(primary_key=True)
    last_id: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), default="0" * 64)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(12), default="info")
    kind: Mapped[str] = mapped_column(String(40), default="")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = _js()
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)
    read_at: Mapped[datetime | None] = _ts(nullable=True)
    delivered: Mapped[dict[str, Any]] = _js()


class KillSwitch(Base):
    __tablename__ = "kill_switches"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scope_type: Mapped[str] = mapped_column(String(12))
    scope_value: Mapped[str] = mapped_column(String(80), default="")
    engaged_at: Mapped[datetime] = _ts(default=utcnow)
    engaged_by: Mapped[str] = mapped_column(String(80), default="")
    reason: Mapped[str] = mapped_column(String(500), default="")
    released_at: Mapped[datetime | None] = _ts(nullable=True)
    released_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    release_note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class JobLease(Base):
    __tablename__ = "job_leases"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    holder: Mapped[str] = mapped_column(String(80), default="")
    lease_until: Mapped[datetime | None] = _ts(nullable=True)
    last_run_at: Mapped[datetime | None] = _ts(nullable=True)
    last_ok_at: Mapped[datetime | None] = _ts(nullable=True)
    last_status: Mapped[str] = mapped_column(String(16), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    runs: Mapped[int] = mapped_column(Integer, default=0)


# =================================================================== мониторинг ===
class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="rss")  # rss|atom|sitemap|html_list|json_feed|manual
    url: Mapped[str] = mapped_column(String(1000), default="")
    site_url: Mapped[str] = mapped_column(String(300), default="")
    country: Mapped[str] = mapped_column(String(8), default="KZ")
    languages: Mapped[list[str]] = _js(list)
    tier: Mapped[str] = mapped_column(String(16), default="quality")  # primary|quality|specialized|aggregator
    category_hint: Mapped[str] = mapped_column(String(40), default="")
    reliability: Mapped[float] = mapped_column(Float, default=0.7)
    independence_group: Mapped[str] = mapped_column(String(64), default="")
    aliases: Mapped[list[str]] = _js(list)  # как источник называют в текстах («Kazinform», «Казинформ»)
    is_official: Mapped[bool] = mapped_column(Boolean, default=False)
    is_wire: Mapped[bool] = mapped_column(Boolean, default=False)
    paywalled: Mapped[bool] = mapped_column(Boolean, default=False)
    fulltext_policy: Mapped[str] = mapped_column(String(16), default="feed_only")  # feed_only | fetch_allowed
    media_policy: Mapped[str] = mapped_column(String(16), default="none")  # none|attribution|official_free|cc
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_url: Mapped[bool] = mapped_column(Boolean, default=False)
    poll_interval_min: Mapped[int] = mapped_column(Integer, default=15)
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Almaty")
    config: Mapped[dict[str, Any]] = _js()
    state: Mapped[dict[str, Any]] = _js()  # etag / last-modified
    notes: Mapped[str] = mapped_column(Text, default="")
    last_fetch_at: Mapped[datetime | None] = _ts(nullable=True)
    last_success_at: Mapped[datetime | None] = _ts(nullable=True)
    last_item_at: Mapped[datetime | None] = _ts(nullable=True)
    last_status: Mapped[str] = mapped_column(String(300), default="")
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    items_last_fetch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("project_id", "url_hash"),
        Index("ix_articles_proj_pub", "project_id", "published_at"),
        Index("ix_articles_proj_event", "project_id", "event_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    source_id: Mapped[int] = _fk("sources")
    event_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    url: Mapped[str] = mapped_column(String(1500))
    canonical_url: Mapped[str] = mapped_column(String(1500))
    url_hash: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(600))
    summary: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[str] = mapped_column(String(8), default="ru")
    author: Mapped[str] = mapped_column(String(200), default="")
    section: Mapped[str] = mapped_column(String(120), default="")
    tags: Mapped[list[str]] = _js(list)
    published_at: Mapped[datetime] = _ts()
    fetched_at: Mapped[datetime] = _ts(default=utcnow)
    images: Mapped[list[dict[str, Any]]] = _js(list)
    videos: Mapped[list[dict[str, Any]]] = _js(list)
    related_links: Mapped[list[str]] = _js(list)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    simhash: Mapped[str] = mapped_column(String(16), default="")
    features: Mapped[dict[str, Any]] = _js()  # entities, numbers, dates, quotes, attribution, flags
    relation: Mapped[str] = mapped_column(String(16), default="original")
    relation_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    similarity: Mapped[float] = mapped_column(Float, default=0.0)
    independent: Mapped[bool] = mapped_column(Boolean, default=True)
    has_full_text: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[Source] = relationship(lazy="joined", innerjoin=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_proj_update", "project_id", "last_update_at"),
        Index("ix_events_proj_stage", "project_id", "stage"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    title: Mapped[str] = mapped_column(String(600))
    summary: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = _ts()
    first_published_at: Mapped[datetime] = _ts()
    last_update_at: Mapped[datetime] = _ts()
    geo: Mapped[str] = mapped_column(String(8), default="world")
    geo_bucket: Mapped[str] = mapped_column(String(8), default="WORLD")
    category: Mapped[str] = mapped_column(String(40), default="other")
    subtopics: Mapped[list[str]] = _js(list)
    event_type: Mapped[str] = mapped_column(String(40), default="report")
    keywords: Mapped[list[str]] = _js(list)
    primary_article_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    primary_source_url: Mapped[str] = mapped_column(String(1500), default="")
    n_articles: Mapped[int] = mapped_column(Integer, default=0)
    n_sources: Mapped[int] = mapped_column(Integer, default=0)
    n_independent: Mapped[int] = mapped_column(Integer, default=0)
    languages: Mapped[list[str]] = _js(list)
    kz_relevance: Mapped[float] = mapped_column(Float, default=0.0)
    world_relevance: Mapped[float] = mapped_column(Float, default=0.0)
    trend_score: Mapped[float] = mapped_column(Float, default=0.0)
    components: Mapped[dict[str, Any]] = _js()
    velocity: Mapped[float] = mapped_column(Float, default=0.0)
    phase: Mapped[str] = mapped_column(String(12), default="emerging")
    verification_status: Mapped[str] = mapped_column(String(20), default="")
    potential_interest: Mapped[float] = mapped_column(Float, default=0.0)
    potential_value: Mapped[float] = mapped_column(Float, default=0.0)
    angle_ideas: Mapped[list[str]] = _js(list)
    flags: Mapped[dict[str, Any]] = _js()
    features: Mapped[dict[str, Any]] = _js()
    stage: Mapped[str] = mapped_column(String(12), default="pool")  # pool|s50|s15|s10|s5|published|dropped
    merged_into_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_scored_at: Mapped[datetime | None] = _ts(nullable=True)
    last_clustered_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class EventSnapshot(Base):
    __tablename__ = "event_snapshots"
    __table_args__ = (Index("ix_snap_event_ts", "event_id", "ts"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = _fk("events")
    ts: Mapped[datetime] = _ts(default=utcnow)
    n_articles: Mapped[int] = mapped_column(Integer, default=0)
    n_sources: Mapped[int] = mapped_column(Integer, default=0)
    n_independent: Mapped[int] = mapped_column(Integer, default=0)
    trend_score: Mapped[float] = mapped_column(Float, default=0.0)
    velocity: Mapped[float] = mapped_column(Float, default=0.0)


class FunnelRun(Base):
    __tablename__ = "funnel_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    started_at: Mapped[datetime] = _ts(default=utcnow)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="running")
    mode: Mapped[str] = mapped_column(String(12), default="learning")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    params: Mapped[dict[str, Any]] = _js()
    counts: Mapped[dict[str, Any]] = _js()
    geo_ratio: Mapped[dict[str, Any]] = _js()
    notes: Mapped[dict[str, Any]] = _js()


class FunnelItem(Base):
    __tablename__ = "funnel_items"
    __table_args__ = (Index("ix_funnel_items_run_stage", "run_id", "stage"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = _fk("funnel_runs")
    event_id: Mapped[int] = _fk("events")
    stage: Mapped[str] = mapped_column(String(12))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    geo_bucket: Mapped[str] = mapped_column(String(8), default="")
    decision: Mapped[str] = mapped_column(String(12), default="kept")  # kept | dropped
    reasons: Mapped[list[str]] = _js(list)


class Verification(Base):
    __tablename__ = "verifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    event_id: Mapped[int] = _fk("events")
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    primary_source: Mapped[dict[str, Any]] = _js()
    n_independent: Mapped[int] = mapped_column(Integer, default=0)
    checks: Mapped[list[dict[str, Any]]] = _js(list)
    facts: Mapped[list[dict[str, Any]]] = _js(list)
    interpretations: Mapped[list[dict[str, Any]]] = _js(list)
    contradictions: Mapped[list[dict[str, Any]]] = _js(list)
    warnings: Mapped[list[str]] = _js(list)
    verified_at: Mapped[datetime] = _ts(default=utcnow)


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (Index("ix_proposals_proj_status", "project_id", "status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slot: Mapped[int] = mapped_column(Integer, default=0)  # №1..№5
    event_id: Mapped[int] = _fk("events")
    verification_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    card: Mapped[dict[str, Any]] = _js()
    prediction: Mapped[dict[str, Any]] = _js()
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    shown_at: Mapped[datetime] = _ts(default=utcnow)
    resolved_at: Mapped[datetime | None] = _ts(nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    content_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replaced_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overrides: Mapped[dict[str, Any]] = _js()  # angle / format / language / emphasis
    expanded: Mapped[dict[str, Any]] = _js()  # «Раскрой подробнее»
    autopilot: Mapped[bool] = mapped_column(Boolean, default=False)


class UserAction(Base):
    __tablename__ = "user_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = _js()
    raw_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)


# ============================================================= профиль / обучение ===
class ProfileFeature(Base):
    """Бета-распределение предпочтений пользователя по признаку (dimension:key)."""

    __tablename__ = "profile_features"
    __table_args__ = (UniqueConstraint("project_id", "dimension", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    dimension: Mapped[str] = mapped_column(String(24))
    key: Mapped[str] = mapped_column(String(200))
    alpha: Mapped[float] = mapped_column(Float, default=0.0)
    beta: Mapped[float] = mapped_column(Float, default=0.0)
    n: Mapped[int] = mapped_column(Integer, default=0)
    last_signal_at: Mapped[datetime | None] = _ts(nullable=True)
    recent: Mapped[list[str]] = _js(list)  # ISO-даты последних положительных выборов (для «серий»)


class ProfileSettings(Base):
    __tablename__ = "profile_settings"
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict[str, Any]] = _js()
    updated_at: Mapped[datetime] = _ts(default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(80), default="")


class LearningEvent(Base):
    __tablename__ = "learning_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    ts: Mapped[datetime] = _ts(default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(20))  # user_action | edit | performance
    signal: Mapped[str] = mapped_column(String(40))
    deltas: Mapped[list[Any]] = _js(list)  # [[dimension, key, dα, dβ], ...]
    ref_type: Mapped[str] = mapped_column(String(20), default="")
    ref_id: Mapped[str] = mapped_column(String(40), default="")
    note: Mapped[str] = mapped_column(String(300), default="")


class EditorialCorrection(Base):
    __tablename__ = "editorial_corrections"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    content_pk: Mapped[int] = _fk("contents")
    platform: Mapped[str] = mapped_column(String(16), default="")
    field: Mapped[str] = mapped_column(String(16))
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")
    stats: Mapped[dict[str, Any]] = _js()
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class PerformanceStat(Base):
    """Реакция аудитории по признаку: среднее log-отношение результата к базовому уровню аккаунта."""

    __tablename__ = "performance_stats"
    __table_args__ = (UniqueConstraint("project_id", "dimension", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    dimension: Mapped[str] = mapped_column(String(24))
    key: Mapped[str] = mapped_column(String(200))
    n: Mapped[int] = mapped_column(Integer, default=0)
    sum_log_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    sum_sq: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = _ts(default=utcnow)


# ==================================================================== контент ===
class ContentSequence(Base):
    __tablename__ = "content_sequences"
    year: Mapped[int] = mapped_column(primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, default=0)


class Content(Base):
    __tablename__ = "contents"
    __table_args__ = (Index("ix_contents_proj_created", "project_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[str] = mapped_column(String(24), unique=True)  # Content-2026-000125
    project_id: Mapped[int] = _fk("projects")
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(600))
    category: Mapped[str] = mapped_column(String(40), default="other")
    geo: Mapped[str] = mapped_column(String(8), default="kz")
    subtopics: Mapped[list[str]] = _js(list)
    language: Mapped[str] = mapped_column(String(8), default="ru")
    angle: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    generator: Mapped[str] = mapped_column(String(60), default="heuristic")
    fact_base: Mapped[list[dict[str, Any]]] = _js(list)
    sources: Mapped[list[dict[str, Any]]] = _js(list)
    draft: Mapped[dict[str, Any]] = _js()
    prediction: Mapped[dict[str, Any]] = _js()
    sensitivity: Mapped[dict[str, Any]] = _js()
    requires_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    origin: Mapped[str] = mapped_column(String(12), default="user")  # user | autopilot
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_event_stage: Mapped[str] = mapped_column(String(40), default="")  # «новая стадия» уже известного события
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow)
    versions: Mapped[list[PlatformVersion]] = relationship(back_populates="content", cascade="all, delete-orphan")


class PlatformVersion(Base):
    __tablename__ = "platform_versions"
    __table_args__ = (Index("ix_pv_content_platform", "content_pk", "platform"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    content_pk: Mapped[int] = _fk("contents")
    platform: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    format: Mapped[str] = mapped_column(String(16), default="post")  # post|photo|gallery|carousel|reels|stories|link
    title: Mapped[str] = mapped_column(String(600), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list[str]] = _js(list)
    media: Mapped[list[dict[str, Any]]] = _js(list)  # [{asset_id, role}]
    extras: Mapped[dict[str, Any]] = _js()  # slides, reels_script, link, …
    language: Mapped[str] = mapped_column(String(8), default="ru")
    text_hash: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(80), default="ai")
    created_at: Mapped[datetime] = _ts(default=utcnow)
    content: Mapped[Content] = relationship(back_populates="versions")


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    kind: Mapped[str] = mapped_column(String(12), default="image")  # image | video | card
    storage_path: Mapped[str] = mapped_column(String(500), default="")
    public_token: Mapped[str] = mapped_column(String(64), unique=True)
    mime: Mapped[str] = mapped_column(String(60), default="image/jpeg")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    source_article_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str] = mapped_column(String(1500), default="")
    rights_status: Mapped[str] = mapped_column(String(16), default="unknown")  # own|generated|official_free|licensed|unknown|restricted
    license: Mapped[str] = mapped_column(String(200), default="")
    attribution: Mapped[str] = mapped_column(String(300), default="")
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_label_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    rights_confirmed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    alt_text: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)


class CheckReport(Base):
    __tablename__ = "check_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    content_pk: Mapped[int] = _fk("contents")
    platform_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(16), default="")
    run_at: Mapped[datetime] = _ts(default=utcnow)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    blocks_autopilot: Mapped[bool] = mapped_column(Boolean, default=False)
    results: Mapped[list[dict[str, Any]]] = _js(list)
    summary: Mapped[str] = mapped_column(String(500), default="")


# ================================================================== публикации ===
class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    platform: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str] = mapped_column(String(120), default="")
    handle: Mapped[str] = mapped_column(String(200), default="")
    secret_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scopes: Mapped[list[str]] = _js(list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    mode: Mapped[str] = mapped_column(String(12), default="manual")
    settings: Mapped[dict[str, Any]] = _js()
    connected_by: Mapped[str] = mapped_column(String(80), default="")
    connected_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    token_expires_at: Mapped[datetime | None] = _ts(nullable=True)
    last_checked_at: Mapped[datetime | None] = _ts(nullable=True)
    last_error: Mapped[str] = mapped_column(String(500), default="")
    rate_info: Mapped[dict[str, Any]] = _js()


class AutopilotPolicy(Base):
    __tablename__ = "autopilot_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str] = mapped_column(String(12), default="manual")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    constraints: Mapped[dict[str, Any]] = _js()
    updated_at: Mapped[datetime] = _ts(default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(80), default="")


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (
        Index("ix_pub_state_sched", "state", "scheduled_at"),
        Index("ix_pub_content", "content_pk", "platform"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    content_pk: Mapped[int] = _fk("contents")
    platform_version_id: Mapped[int] = _fk("platform_versions")
    platform: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("platform_accounts.id"), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="draft")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    schedule: Mapped[dict[str, Any]] = _js()
    scheduled_at: Mapped[datetime | None] = _ts(nullable=True)
    next_attempt_at: Mapped[datetime | None] = _ts(nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    approved_at: Mapped[datetime | None] = _ts(nullable=True)
    origin: Mapped[str] = mapped_column(String(12), default="user")
    external_id: Mapped[str] = mapped_column(String(200), default="")
    external_url: Mapped[str] = mapped_column(String(500), default="")
    published_at: Mapped[datetime | None] = _ts(nullable=True)
    last_error: Mapped[dict[str, Any]] = _js()
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str] = mapped_column(String(40), default="")
    claimed_at: Mapped[datetime | None] = _ts(nullable=True)
    progress: Mapped[dict[str, Any]] = _js()
    needs_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow)


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"
    __table_args__ = (UniqueConstraint("publication_id", "attempt_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = _fk("publications")
    attempt_no: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = _ts(default=utcnow)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), default="started")
    error_class: Mapped[str] = mapped_column(String(40), default="")
    error_code: Mapped[str] = mapped_column(String(60), default="")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(String(600), default="")
    response_excerpt: Mapped[str] = mapped_column(String(600), default="")
    external_id: Mapped[str] = mapped_column(String(200), default="")
    progress: Mapped[dict[str, Any]] = _js()
    reconcile: Mapped[dict[str, Any]] = _js()
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


class PublicationEvent(Base):
    __tablename__ = "publication_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = _fk("publications")
    ts: Mapped[datetime] = _ts(default=utcnow)
    from_state: Mapped[str] = mapped_column(String(20), default="")
    to_state: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(String(500), default="")


# ==================================================================== аналитика ===
class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    __table_args__ = (Index("ix_metrics_pub_ts", "publication_id", "captured_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = _fk("publications")
    captured_at: Mapped[datetime] = _ts(default=utcnow)
    age_hours: Mapped[float] = mapped_column(Float, default=0.0)
    views: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reach: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    likes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comments: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    saves: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    clicks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    follows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    extra: Mapped[dict[str, Any]] = _js()
    source: Mapped[str] = mapped_column(String(12), default="api")  # api | manual | import
    unavailable: Mapped[list[str]] = _js(list)


class AudienceSnapshot(Base):
    __tablename__ = "audience_snapshots"
    __table_args__ = (Index("ix_aud_acc_ts", "account_id", "ts"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = _fk("platform_accounts")
    ts: Mapped[datetime] = _ts(default=utcnow)
    followers: Mapped[int] = mapped_column(Integer, default=0)
    extra: Mapped[dict[str, Any]] = _js()


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    kind: Mapped[str] = mapped_column(String(12))  # weekly | monthly | strategy
    period_start: Mapped[datetime] = _ts()
    period_end: Mapped[datetime] = _ts()
    payload: Mapped[dict[str, Any]] = _js()
    markdown: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)


class ForecastRecord(Base):
    __tablename__ = "forecast_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = _fk("projects")
    content_pk: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="")
    prediction: Mapped[dict[str, Any]] = _js()
    actual: Mapped[dict[str, Any]] = _js()
    error: Mapped[dict[str, Any]] = _js()
    created_at: Mapped[datetime] = _ts(default=utcnow)
    evaluated_at: Mapped[datetime | None] = _ts(nullable=True)


# ======================================================================== ops ===
class HealthRecord(Base):
    __tablename__ = "health_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts(default=utcnow, index=True)
    component: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(12))
    detail: Mapped[dict[str, Any]] = _js()


class BackupRecord(Base):
    __tablename__ = "backup_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts(default=utcnow)
    kind: Mapped[str] = mapped_column(String(12), default="db")
    path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = _ts(nullable=True)
    verify_status: Mapped[str] = mapped_column(String(12), default="")
    note: Mapped[str] = mapped_column(String(300), default="")


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts(default=utcnow, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task: Mapped[str] = mapped_column(String(60))
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="ok")  # ok | error | bad_json | schema_mismatch | budget_exceeded
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(300), default="")


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    applied_at: Mapped[datetime] = _ts(default=utcnow)
