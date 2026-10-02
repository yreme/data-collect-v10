#!/usr/bin/env bash
# =============================================================================
# 原生安装 zenohd（不依赖 docker），安装为 systemd 服务 sensorhub-zenohd
#
#   sudo ./install.sh                         # 服务器 A
#   sudo ./install.sh --config zenohd.server-d.json5   # 服务器 D
#   sudo ./install.sh --offline /path/zenoh-1.10.1-x86_64-unknown-linux-gnu-standalone.zip
#
# 安装内容：
#   /opt/zenoh/bin/zenohd               可执行文件
#   /opt/zenoh/lib/*.so                 插件（rest / storage_manager）
#   /etc/sensorhub/zenohd.json5         配置（已存在则备份为 .bak.<时间>）
#   /etc/systemd/system/sensorhub-zenohd.service
# =============================================================================
set -euo pipefail

VERSION="${ZENOH_VERSION:-1.10.1}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) TRIPLE="x86_64-unknown-linux-gnu" ;;
  aarch64) TRIPLE="aarch64-unknown-linux-gnu" ;;
  *) echo "不支持的架构 $ARCH" >&2; exit 1 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG_NAME="zenohd.json5"
OFFLINE_ZIP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_NAME="$2"; shift 2 ;;
    --offline) OFFLINE_ZIP="$2"; shift 2 ;;
    -h|--help) sed -n 2,16p "$0"; exit 0 ;;
    *) echo "未知参数 $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "请用 sudo 运行" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ZIP="$OFFLINE_ZIP"
if [[ -z "$ZIP" ]]; then
  URL="https://github.com/eclipse-zenoh/zenoh/releases/download/${VERSION}/zenoh-${VERSION}-${TRIPLE}-standalone.zip"
  echo ">> 下载 $URL"
  curl -fSL --retry 3 -o "$TMP/zenoh.zip" "$URL"
  ZIP="$TMP/zenoh.zip"
fi

echo ">> 解压到 /opt/zenoh"
mkdir -p /opt/zenoh/bin /opt/zenoh/lib /etc/sensorhub
unzip -o -q "$ZIP" -d "$TMP/x"
install -m 755 "$TMP/x/zenohd" /opt/zenoh/bin/zenohd
find "$TMP/x" -maxdepth 1 -name 'libzenoh_plugin_*.so' -exec install -m 644 {} /opt/zenoh/lib/ \;

if [[ -f /etc/sensorhub/zenohd.json5 ]]; then
  cp /etc/sensorhub/zenohd.json5 "/etc/sensorhub/zenohd.json5.bak.$(date +%Y%m%d-%H%M%S)"
fi
install -m 644 "$HERE/../config/$CONFIG_NAME" /etc/sensorhub/zenohd.json5

install -m 644 "$HERE/sensorhub-zenohd.service" /etc/systemd/system/sensorhub-zenohd.service
systemctl daemon-reload
systemctl enable --now sensorhub-zenohd
sleep 2
systemctl --no-pager status sensorhub-zenohd | head -n 12 || true
echo ">> 验证: curl http://127.0.0.1:8000/@/*/router"
curl -fsS "http://127.0.0.1:8000/@/*/router" >/dev/null && echo "OK zenohd ${VERSION} 已运行"
