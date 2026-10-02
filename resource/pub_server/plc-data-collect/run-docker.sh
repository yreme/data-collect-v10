#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

mkdir -p data/mcap/hourly data/mcap/short

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

case "${1:-up}" in
  up)
    docker compose up -d --build
    ;;
  down)
    docker compose down
    ;;
  restart)
    docker compose restart crane-monitor
    ;;
  logs)
    docker compose logs -f crane-monitor
    ;;
  *)
    echo "Usage: $0 [up|down|restart|logs]"
    exit 1
    ;;
esac
