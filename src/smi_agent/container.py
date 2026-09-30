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
        self._llm_override = llm
        self._wire()

    def _wire(self) -> None:
        from .events.cluster import EventService
        from .ingestion.service import IngestService

        from .analytics.metrics import MetricsService
        from .security.auth import AuthService
        from .analytics.service import AnalyticsService
        from .checks.service import CheckService
        from .content.service import ContentService
        from .funnel.service import FunnelService
        from .interaction.service import InteractionService
        from .learning.service import LearningService
        from .profile.service import ProfileService
        from .publishing.accounts import AccountService
        from .publishing.autopilot import AutopilotService
        from .publishing.killswitch import KillSwitchService
        from .publishing.scheduling import SchedulingService
        from .publishing.service import PublishingService
        from .scoring.trend import TrendScorer
        from .verification.service import VerificationService

        from .ops.backup import BackupService
        from .ops.health import HealthService
        from .llm.client import build_client
        from .llm.service import LlmService

        client = self._llm_override
        if client is None:
            client = build_client(self.settings)
        if client is None and self.settings.demo_mode and not self.settings.is_production:
            from .demo.llm import DemoLlm

            client = DemoLlm()  # только для вымышленных демо-новостей; на остальное честно не отвечает
        self.llm = LlmService(self, client)
        self.ingest = IngestService(self)
        self.events = EventService(self)
        self.scoring = TrendScorer(self)
        self.verification = VerificationService(self)
        self.learning = LearningService(self)
        self.profile = ProfileService(self)
        self.funnel = FunnelService(self)
        self.checks = CheckService(self)
        self.content = ContentService(self)
        self.interaction = InteractionService(self)
        self.accounts = AccountService(self)
        self.scheduling = SchedulingService(self)
        self.killswitch = KillSwitchService(self)
        self.publishing = PublishingService(self)
        self.autopilot = AutopilotService(self)
        self.metrics = MetricsService(self)
        self.analytics = AnalyticsService(self)
        self.backup = BackupService(self)
        self.auth = AuthService(self)
        self.health = HealthService(self)

    def close(self) -> None:
        self.http.close()
        self.accounts.http.close()
        self.db.dispose()
