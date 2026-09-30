"""Контейнер зависимостей: единая точка сборки сервисов (API, воркер, CLI и тесты используют один и тот же)."""

from __future__ import annotations

from typing import Any

from .audit import AuditService
from .config import Settings, get_settings
from .core.clock import Clock
from .db import Database
from .knowledge import Knowledge, get_knowledge
from .monitoring.http import SafeHttp
from .security.secrets import SecretStore


class Container:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        db: Database | None = None,
        http: SafeHttp | None = None,
        know: Knowledge | None = None,
        llm: Any = None,
    ):
        self.settings = settings or get_settings()
        self.clock = clock or Clock()
        self.db = db or Database(self.settings.database_url)
        self.know = know or get_knowledge(str(self.settings.config_dir) if self.settings.config_dir.exists() else None)
        self.audit = AuditService(self.clock)
        self.secrets = SecretStore(self.settings, self.audit, self.clock)
        self.http = http or SafeHttp(self.settings)
        self._llm = llm
        self._wire()

    def _wire(self) -> None:
        from .events.cluster import EventService
        from .ingestion.service import IngestService

        from .funnel.service import FunnelService
        from .learning.service import LearningService
        from .profile.service import ProfileService
        from .scoring.trend import TrendScorer
        from .verification.service import VerificationService

        self.ingest = IngestService(self)
        self.events = EventService(self)
        self.scoring = TrendScorer(self)
        self.verification = VerificationService(self)
        self.learning = LearningService(self)
        self.profile = ProfileService(self)
        self.funnel = FunnelService(self)

    def close(self) -> None:
        self.http.close()
        self.db.dispose()
