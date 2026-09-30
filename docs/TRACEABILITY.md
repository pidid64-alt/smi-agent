# Матрица соответствия ТЗ v1.0 → реализация

Статусы: ✅ реализовано и покрыто автотестами · 🟡 реализовано частично / эвристика · ⚠️ реализовано, но **не проверено с реальной внешней системой** (она недоступна в среде разработки; проверено на подменённых ответах) · ➖ не реализовано.
Нумерация — по «Базовому ТЗ v1.0». Тесты указаны по файлам в `tests/`.

## Процесс и отбор тем (§2, §4–16, §60)

| § | Требование | Реализация | Тесты | Статус |
|---|---|---|---|---|
| §2, §60 | Сквозной поток: мониторинг → события → оценка → 50→15→10→5 → выбор → текст → платформы → проверки → публикация → статистика → обучение | `container.py`, `ops/worker.py`, все модули | `test_demo.py` (сквозной), `test_api.py::test_end_to_end_via_api` | ✅ |
| §4, §10 | Гео-баланс КЗ ≈60% / мир ≈40% как **мягкий приор**; фактическая доля сохраняется в аналитике; Центральная Азия — к КЗ | `funnel/service.py::select_balanced`, `settings_model.GeoSettings`, `analytics/service.py::geo_ratio`, `events/cluster.py` | `test_funnel.py` (приор, без «добивки», ЦА), `test_analytics.py::test_geo_ratio…` | ✅ |
| §5–6 | Источники КЗ (NUR.KZ обязателен, Kazinform, 24KZ, Tengrinews, Informburo, Zakon, Kursiv, Vlast, Kapital), международные (Reuters, AP, BBC, Bloomberg, FT, CNBC, Guardian), профильные и первичные | `defaults/sources.yaml` (28 записей), `ingestion/service.py` | `test_ingestion.py` | 🟡 NUR.KZ и Kursiv проверены вручную, остальные адреса помечены «не проверена»; Reuters — sitemap, AP — лицензионный API (отключён) |
| §6, §59 | Добавление/удаление источников **конфигурацией, без кода**; NUR.KZ нельзя удалить | интерфейс/API/YAML, `IngestService.upsert_source/delete_source` | `test_ingestion.py::test_source_crud_rules`, `test_sources_are_config_only…`, `test_api.py::test_sources_api_rules` | ✅ |
| §7 | Сбор: RSS/Atom/sitemap/JSON/HTML, нормализация, дедупликация материалов, платные источники — только анонсы | `ingestion/{feeds,normalize,features}.py` | `test_ingestion.py` | ✅ |
| §8.1 | Одно событие + N источников; перепечатки, переводы, обновления | `events/{similarity,cluster}.py` | `test_events.py` (корпус ru/en, перестановки, перепечатки) | ✅ (эвристика) |
| §8–9 | Trend Score: свежесть, независимые источники, скорость роста, масштаб, новизна, значимость КЗ/мир, практическая ценность, интерес аудитории, обсуждение, достаточность фактов, качество источников, выполнимость, историческая эффективность | `scoring/trend.py` (13 весов; значимость КЗ и мировая хранятся отдельно, в сумму входит max) | `test_scoring.py` | ✅ |
| §9 | Пример скорости: 3 → 15 → 40 источников = «быстро набирает популярность»; число публикаций ≠ значимость | `velocity_windows`, происхождение источников | `test_scoring.py`, `test_events.py::test_many_reprints…`, `test_demo.py::test_raw_article_count…` | ✅ |
| §11 | Этап «50» из пула | `funnel/service.py` этап 1 | `test_funnel.py` | ✅ |
| §13 | Этап «15»: дубли, устаревшее, слабое, кликбейт, реклама, неподтверждённое, незначительное, мало фактов | этап 2, причины в `FunnelItem.reasons` | `test_funnel.py` | ✅ |
| §14 | Этап «10»: первоисточник, независимые подтверждения, факты/даты/цифры/цитаты/контекст, противоречия, факт vs интерпретация; сомнительное — отсев или с предупреждением | `verification/service.py` | `test_verification.py` | ✅ |
| §15 | Этап «5»: разнообразие (КЗ, ИИ/технологии, авто, экономика/бизнес, наука/мир/необычное) — мягкий ориентир | `FunnelService.pick_final`, `defaults/taxonomy.yaml::themes` | `test_funnel.py::test_proposals_are_diverse…`, `test_demo.py` | ✅ |
| §15 | Слабые темы не добавляются ради квоты 60/40 и счётчиков 50/15/10/5 | пороги, `select_balanced` | `test_funnel.py::test_quota_is_never_padded…`, `test_only_one_good_topic…` | ✅ |
| §16 | Карточка: название, почему сейчас, что произошло (2–4 факта), почему интересно, источники, степень проверки, угол, формат; бейджи проверки | `proposals/cards.py`, интерфейс «5 предложений» | `test_funnel.py::test_proposal_card_has_all_required_sections` | ✅ |
| §17 | Команды: «Беру №2», «№4, но акцент на Казахстан», «№1 неинтересна», «Замени №3», «Раскрой №5 подробнее» | `interaction/commands.py`, `interaction/service.py` | `test_commands.py` | ✅ (ru/en; kk — частично) |
| §51 | Повтор или «новая стадия» уже опубликованной темы | `funnel/repeat.py` | `test_funnel.py` (повторы), `test_content.py` | ✅ |

## Обучение и профиль (§18–19, §44–45)

| § | Требование | Реализация | Тесты | Статус |
|---|---|---|---|---|
| §18, §44 | Каждое действие — сигнал обучения; анализ выбора пользователя | `learning/service.py`, `profile/service.py::choice_analysis` | `test_learning.py`, `test_commands.py` | ✅ |
| §19, §45 | Редакционный профиль: предпочтения, стиль, явные настройки; «что я понял» | `profile/service.py`, интерфейс «Редакционный профиль» | `test_learning.py` | ✅ |
| §19 | Обзор/пересмотр стратегии | `analytics/service.py::build_report('strategy')`, рекомендации | `test_analytics.py::test_weekly_report…` | 🟡 рекомендации по правилам |
| §45 | Правки пользователя учитываются (длина, хэштеги, эмодзи, тон) | `LearningService.record_edit`, `style_hints` | `test_content.py::test_edit_creates_version…`, `test_learning.py` | ✅ |

## Контент (§20–23, §30–32, §50, §53–54, §62)

| § | Требование | Реализация | Тесты | Статус |
|---|---|---|---|---|
| §20–22 | Оригинальный текст, без копирования и механического рерайта; собственные выводы не приписываются источникам | `content/factbase.py`, `generator.py`, `originality.py`, проверки `originality`, `own_opinion`, `attribution` | `test_content.py` | ✅ (**качество реальной LLM не проверялось**) |
| §22 | Без LLM нельзя «подделать» оригинальность | эвристика = заготовка, не проходит проверку оригинальности, блокирует автопилот | `test_content.py::test_heuristic_draft_is_honest…` | ✅ |
| §23 | Нативные версии TG / IG / FB (а не копии) | `content/adapters.py`, промпты по платформам | `test_content.py::test_llm_draft_is_original_native…` | ✅ |
| §30–31 | Визуал: оригинальные карточки; ИИ-изображения не вводят в заблуждение (метка «Создано ИИ», метаданные) | `visual/cards.py`, проверка `media` | `test_content.py`, `test_adapters.py` | 🟡 карточки — да; **загрузка собственных изображений/генерация ИИ-изображений не реализована**, механизм метки и проверки готов |
| §32 | Проверки перед публикацией; любая проваленная проверка запрещает автопубликацию | `checks/service.py`, предполётные проверки при захвате | `test_content.py`, `test_publishing.py::test_preflight_rechecks…` | ✅ |
| §50 | База знаний: факты, источники, профили, история решений | таблицы `articles/events/verifications/contents/learning_events/…`, `Content.fact_base` | все | ✅ |
| §53 | Политика: нейтральный тон, без агитации, оценок, прогнозов выборов; спорное — с атрибуцией; только ручное подтверждение | `political_agitation` (лексикон), проверка `politics`, `requires_manual`, блок автопилота | `test_content.py::test_political_topics…`, `test_autopilot.py::test_politics_and_sensitive…` | 🟡 лексиконная проверка; тонкая нейтральность — задача редактора/LLM |
| §54 | Языки ru / kk / en | язык контента, метки платформ, лексиконы ru/kk/en, интерфейс (ru полностью; kk/en — навигация и ключевые кнопки) | `test_text.py`, `test_content.py` | 🟡 без LLM перевод недоступен (честная пометка) |
| §62 | Не агрегатор, не рерайтер, не авторепостер | принципы выше; текст только из базы фактов; автопилот ограничен | `test_content.py`, `test_autopilot.py` | ✅ |

## Публикация (§24–35, §39, §52)

| § | Требование | Реализация | Тесты | Статус |
|---|---|---|---|---|
| §24 | Отдельный модуль публикации; только официальные API; пароли внешних платформ не хранятся | `publishing/*`, `AccountService.connect` (токены) | `test_publishing.py::test_passwords_of_external…`, `test_adapters.py` | ✅ (логика); ⚠️ реальные API |
| §24, §25 | Content ID `Content-2026-000125` | `core/ids.py` | `test_publishing.py`, `test_content.py` | ✅ |
| §24, §35 | 8 состояний: Черновик, Ожидает подтверждения, Требует проверки, Запланировано, Публикуется, Опубликовано, Ошибка, Отменено | `publishing/service.py::TRANSITIONS` | `test_publishing.py::test_state_machine…` | ✅ |
| §24 | Режимы по платформам: ручное подтверждение / по расписанию / автоматически | `PlatformAccount.mode` | `test_publishing.py`, `test_autopilot.py` | ✅ |
| §26 | Планирование, «Оптимальное время», тихие часы, интервалы | `publishing/scheduling.py` | `test_publishing.py::test_scheduling_modes`, `test_autopilot.py` | ✅ (при малых данных — часы по умолчанию) |
| §27–29 | Особенности TG/IG/FB: лимиты длины, хэштеги, карусель ≤10, Reels, JPEG | `content/platforms.py`, `publishing/adapters/*` | `test_adapters.py`, `test_content.py` | ⚠️ Stories не реализованы; Reels требует готового видео |
| §33 | Мониторинг публикаций и сбор метрик по возрастам | `analytics/metrics.py` | `test_analytics.py` | ⚠️ реальные метрики платформ; TG Bot API статистики не даёт → импорт/ручной ввод |
| §34 | Идемпотентность: повтор только после доказанного «не опубликовано»; сверка | `publishing/service.py` (ключ, CAS, сверка, `recover_stuck`) | `test_publishing.py` (одновременные воркеры, неизвестный исход, потерянный ответ), `test_adapters.py` | ✅ |
| §35 | Ошибки: классификация, повторы с паузой, лимиты платформ; сбой одной платформы не блокирует другие | `publishing/errors.py`, `_record_outcome` | `test_publishing.py::test_failure_modes…`, `test_one_platform_failure…` | ✅ |
| §39 | Автопилот выключен по умолчанию; политика/чувствительное — только вручную | `publishing/autopilot.py` | `test_autopilot.py` | ✅ |
| §39 | Аварийный выключатель: система/проект/аккаунт/платформа/категория; снятие — только явным действием с правом | `publishing/killswitch.py` | `test_autopilot.py::test_kill_switch_*` | ✅ |
| §52 | Режимы: Обучение / Со-редактор / Автопилот | `AutopilotService.set_mode/tick` | `test_autopilot.py` | ✅ |

## Безопасность и эксплуатация (§36–38, §55–59)

| § | Требование | Реализация | Тесты | Статус |
|---|---|---|---|---|
| §36 | TLS | прокси с TLS (`deploy/Caddyfile`), `Secure`-cookie и HSTS в production, проверка `public_url` | `test_api.py::test_mfa_required_for_admin_in_production`, `test_ops.py::test_config_lint…` | 🟡 TLS терминируется прокси; не проверялось в среде |
| §36 | OAuth | OAuth Meta (подписанный `state`, обмен кода); основной путь — токены API | `publishing/accounts.py::MetaOAuth` | ⚠️ только на моках |
| §36 | MFA, наименьшие привилегии, изоляция проектов | TOTP (RFC 6238, защита от повтора), RBAC (4 роли), `project_id` | `test_security.py`, `test_api.py` | ✅ |
| §36 | SSRF, CSRF | `security/ssrf.py` (схемы, порты, DNS, закрепление IP, редиректы), CSRF-токен + Origin, строгий CSP | `test_security.py`, `test_ingestion.py`, `test_api.py` | ✅ |
| §37 | Секреты не в клиенте, URL, логах, сообщениях, аудите | AES-256-GCM (AAD), `RedactingFilter`, `sanitize_details`, токен только в заголовках | `test_security.py`, `test_db_audit.py`, `test_publishing.py::test_tokens_do_not_leak…`, `test_adapters.py` | ✅ |
| §37–38 | Ротация и отзыв секретов/доступа | `SecretStore.rotate_master_key/revoke`, `AccountService.revoke`, журнал отзывов переприменяется после восстановления | `test_security.py`, `test_ops.py::test_revocations_survive_restore`, `test_publishing.py` | ✅ |
| §38, §56 | Аудит действий (кто/что/когда), неизменяемость | `audit/service.py` (хеш-цепочка, append-only триггеры SQLite/PG), API/экспорт, проверка целостности | `test_db_audit.py`, `test_api.py::test_audit_export…` | ✅ (триггеры PG не запускались) |
| §55 | Роли: Пользователь, Администратор, Аудитор, AI-сервис | `security/rbac.py` | `test_security.py::test_rbac_matrix`, `test_api.py` | ✅ |
| §57 | Мониторинг: компоненты, пороги, оповещения | `ops/health.py` (+ `/api/health`, `/api/metrics`), уведомления | `test_ops.py` | 🟡 оповещения — в интерфейсе и эндпоинтах; внешней доставки (e-mail/мессенджер) нет |
| §58 | Резервное копирование: шифрование, проверка, RPO/RTO | `ops/backup.py` (онлайн-копия SQLite, AES-256-GCM, проверка целостности и цепочки аудита, `restore-test` с замером) | `test_ops.py` | ✅ SQLite; ⚠️ PostgreSQL (`pg_dump`) не запускался |
| §59 | Масштабируемость; добавление платформ/языков/моделей без правки ядра | аренда заданий, окна, точки расширения | `test_ops.py::test_worker_lease…`, [ARCHITECTURE §5–6](ARCHITECTURE.md) | 🟡 проверено на одном узле |

## Аналитика (§40–49)

| § | Требование | Реализация | Тесты | Статус |
|---|---|---|---|---|
| §40–41 | Статистика по публикациям, метрики по возрастам | `analytics/metrics.py` | `test_analytics.py` | ✅ (логика); ⚠️ реальные метрики |
| §42–43 | Поиск закономерностей; дашборд из шести разделов | `analytics/service.py::insights/dashboard` | `test_analytics.py` | ✅ (выводы только при n ≥ 4 на группу, с уровнем уверенности) |
| §46 | Недельные и месячные отчёты | `build_report`, задание воркера `reports` | `test_analytics.py`, `test_ops.py` | ✅ |
| §47 | Прогноз vs факт | `MetricsService.calibration/forecast_accuracy`, график «Прогноз и факт» | `test_analytics.py` | ✅ |
| §48–49 | Графики за сегодня, 7д, 30д, 3м, 6м, год, произвольный период | `AnalyticsService.series`, интерфейс «Аналитика» | `test_analytics.py::test_series_periods…` | ✅ |

## Не реализовано / за рамками первого этапа
WhatsApp (исключён ТЗ) · Stories · генерация видео и ИИ-изображений · загрузка пользовательских медиафайлов · векторные эмбеддинги · внешняя доставка оповещений · версионные миграции БД · проверка с живыми API платформ и PostgreSQL.
