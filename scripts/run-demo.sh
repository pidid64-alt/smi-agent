#!/usr/bin/env bash
# Запуск демо-режима без Docker: вымышленные новости, песочница вместо платформ. Данные — во временном каталоге.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${SMI_DEMO_DIR:-/tmp/smi-demo}"
rm -rf "$DATA" && mkdir -p "$DATA"
export SMI_DEMO_MODE=1 SMI_ENV=dev SMI_DATA_DIR="$DATA" SMI_DATABASE_URL="sqlite:///$DATA/demo.db" SMI_BACKUP_DIR="$DATA/backups" SMI_EMBEDDED_WORKER=1
echo "Интерфейс: http://localhost:${PORT:-8000}   логин: demo   пароль: ${SMI_DEMO_PASSWORD:-smi-agent-showcase}"
exec python -m uvicorn smi_agent.api.app:app_factory --factory --host 127.0.0.1 --port "${PORT:-8000}"
