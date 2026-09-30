"""Настройки проекта (хранятся в projects.settings, валидируются Pydantic, правятся через API/UI с аудитом)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .core.enums import AgentMode

DEFAULT_WEIGHTS: dict[str, float] = {
    "freshness": 0.10,
    "independent_sources": 0.12,
    "velocity": 0.14,
    "scale": 0.07,
    "novelty": 0.06,
    "significance": 0.10,  # max(значимость для КЗ, мировая значимость) с учётом приоритетов аудитории
    "practical_value": 0.10,
    "audience_interest": 0.06,
    "discussion": 0.04,
    "fact_sufficiency": 0.06,
    "source_quality": 0.06,
    "feasibility": 0.04,
    "historical": 0.05,
}


class FunnelSettings(BaseModel):
    lookback_hours: int = 48
    stage1: int = Field(50, ge=1, le=500)
    stage2: int = Field(15, ge=1, le=200)
    stage3: int = Field(10, ge=1, le=100)
    final: int = Field(5, ge=1, le=20)
    min_trend_score: float = 28.0  # нижний порог: слабые события не попадают в «50» ради квоты
    min_facts: int = 2
    max_unverified_in_stage3: int = 2
    max_event_age_hours: int = 72
    repeat_window_days: int = 14  # окно защиты от повторов (§51)


class GeoSettings(BaseModel):
    kz_target: float = Field(0.6, ge=0, le=1)
    world_target: float = Field(0.4, ge=0, le=1)
    balance_strength: float = Field(0.35, ge=0, le=1)  # 0 — чистый рейтинг, 1 — сильная подстройка к целевой доле


class ScoringSettings(BaseModel):
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    freshness_half_life_hours: float = 14.0
    velocity_window_hours: float = 2.0
    independent_saturation: int = 12
    kz_priority: float = 1.0
    world_priority: float = 0.92


class ContentSettings(BaseModel):
    languages: list[str] = ["ru", "kk", "en"]
    default_language: str = "ru"
    platforms: list[str] = ["telegram", "instagram", "facebook"]
    brand_name: str = "Мой медиа-проект"
    ai_disclosure: bool = True  # маркировка ИИ-участия (закон РК «Об ИИ», с 2026 г.)
    ai_disclosure_text: dict[str, str] = {
        "ru": "Материал подготовлен с использованием ИИ.",
        "kk": "Материал жасанды интеллектті пайдаланып дайындалды.",
        "en": "This material was prepared with the help of AI.",
    }
    ai_disclosure_reviewed_suffix: dict[str, str] = {"ru": " Проверено редактором.", "kk": " Редактор тексерді.", "en": " Reviewed by an editor."}
    ai_image_label: dict[str, str] = {"ru": "Создано ИИ", "kk": "ЖИ жасаған", "en": "AI-generated"}
    copy_max_ratio: float = 0.12  # допустимая доля дословных совпадений с источниками
    copy_max_run_words: int = 10


class SchedulingSettings(BaseModel):
    timezone: str = "Asia/Almaty"
    quiet_hours: tuple[int, int] = (0, 7)  # не публиковать автоматически с 00:00 до 07:00 по местному времени
    default_hours: dict[str, list[int]] = {
        "telegram": [9, 13, 19, 21],
        "facebook": [10, 13, 19, 20],
        "instagram": [12, 18, 20, 21],
    }


class AutopilotSettings(BaseModel):
    max_posts_per_day: int = 3
    min_gap_minutes: int = 120
    min_trend_score: float = 45.0
    min_verification: str = "multi_confirmed"
    require_llm_generator: bool = True


class MonitoringSettings(BaseModel):
    poll_interval_min: int = 15
    source_stale_hours: float = 12.0
    dedup_window_hours: int = 72
    max_item_age_days: int = 7
    fulltext_per_poll: int = 8  # лимит дозагрузки полных текстов за один опрос источника


class ProjectSettings(BaseModel):
    mode: AgentMode = AgentMode.LEARNING
    funnel: FunnelSettings = FunnelSettings()
    geo: GeoSettings = GeoSettings()
    scoring: ScoringSettings = ScoringSettings()
    content: ContentSettings = ContentSettings()
    scheduling: SchedulingSettings = SchedulingSettings()
    autopilot: AutopilotSettings = AutopilotSettings()
    monitoring: MonitoringSettings = MonitoringSettings()

    @field_validator("funnel")
    @classmethod
    def _funnel_monotonic(cls, v: FunnelSettings) -> FunnelSettings:
        if not (v.stage1 >= v.stage2 >= v.stage3 >= v.final):
            raise ValueError("Воронка должна сужаться: stage1 ≥ stage2 ≥ stage3 ≥ final")
        return v


def load_project_settings(raw: dict | None) -> ProjectSettings:
    return ProjectSettings.model_validate(raw or {})
