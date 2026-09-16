#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${ENVIRONMENT:-development}" == "production" ]]; then
	exec uvicorn app.main:app --host "${SERVER_HOST:-0.0.0.0}" --port "${SERVER_PORT:-8000}"
fi
exec uvicorn app.main:app --reload --host "${SERVER_HOST:-127.0.0.1}" --port "${SERVER_PORT:-8000}"
