#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/python"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export MCAP_HOURLY_DIR="${MCAP_HOURLY_DIR:-$ROOT/data/mcap/hourly}"
export MCAP_SHORT_DIR="${MCAP_SHORT_DIR:-$ROOT/data/mcap/short}"
mkdir -p "$MCAP_HOURLY_DIR" "$MCAP_SHORT_DIR"

if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "Installing Python dependencies..."
  python3 -m pip install -r requirements.txt
fi

MODE_ARGS=()
if [ "${MOCK_MODE:-false}" = "true" ]; then
  MODE_ARGS+=(--mock)
else
  MODE_ARGS+=(--no-mock)
fi

exec python3 monitor.py \
  --host "${WEB_HOST:-0.0.0.0}" \
  --port "${WEB_PORT:-8080}" \
  --listen-udp "${UDP_PORT:-12730}" \
  "${MODE_ARGS[@]}"
