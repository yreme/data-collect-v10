#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MCAP="${1:-/home/user/data/test-data/full-sample/multimodal_2026-08-06_07-06-00.mcap}"

export PYTHONPATH="${ROOT}/multimodal_src:${ROOT}/imu-sync:${ROOT}/lidar-sync:${ROOT}/client-mcap-plc-web-src:${PYTHONPATH:-}"

exec python3 "${ROOT}/client-mcap-plc-web-src/tools/mcap_mock_shm_replay.py" \
  "${MCAP}" \
  --loop \
  --hz 2 \
  --max-frames 120 \
  "${@:2}"
