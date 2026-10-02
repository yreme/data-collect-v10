#!/usr/bin/env bash
# 前台运行 zenohd（调试用，不安装 systemd）。
#   ./run.sh                       # 使用 ../config/zenohd.json5
#   ZENOHD_BIN=/tmp/zenohd/zenohd ./run.sh ../config/zenohd.server-d.json5
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="${1:-$HERE/../config/zenohd.json5}"
BIN="${ZENOHD_BIN:-$(command -v zenohd || echo /opt/zenoh/bin/zenohd)}"

if ! ulimit -l unlimited 2>/dev/null; then
  echo "警告: 无法设置 ulimit -l unlimited（当前 $(ulimit -l) KB），SHM 可能失效；请用 root 或在 /etc/security/limits.conf 放开 memlock" >&2
fi
exec "$BIN" -c "$CONF"
