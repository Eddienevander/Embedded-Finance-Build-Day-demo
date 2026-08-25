#!/usr/bin/env bash
# Trust Layer demo launcher: uv sync + seed + pre-flight + serve.
# Requirements on a clean machine: uv installed, ANTHROPIC_API_KEY set.
set -euo pipefail
cd "$(dirname "$0")"

export MOCK_MODE="${MOCK_MODE:-true}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

uv sync
uv run python -m app.seed

# Fail loudly here rather than mid-demo. SKIP_PREFLIGHT=true runs anyway
# (replay mode needs no API access).
if ! uv run python -m app.preflight; then
  if [ "${SKIP_PREFLIGHT:-false}" != "true" ]; then
    exit 1
  fi
  echo "  … starting anyway (SKIP_PREFLIGHT=true) — use Replay mode in the UI"
fi

echo
echo "  Trust Layer dashboard →  http://${HOST}:${PORT}/"
echo

exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT"
