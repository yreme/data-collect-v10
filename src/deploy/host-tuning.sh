#!/usr/bin/env bash
# 宿主机网络/内存调优（GigE 相机 + 10GbE + Zenoh）。需要 root；--persist 写入 /etc/sysctl.d。
#   sudo ./host-tuning.sh --iface enp1s0f0 [--mtu 9000] [--persist]
set -euo pipefail

IFACE=""
MTU=9000
PERSIST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --iface) IFACE="$2"; shift 2 ;;
    --mtu) MTU="$2"; shift 2 ;;
    --persist) PERSIST=1; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "需要 root" >&2; exit 1; }

SYSCTL=(
  "net.core.rmem_max=134217728"
  "net.core.wmem_max=134217728"
  "net.core.rmem_default=33554432"
  "net.core.netdev_max_backlog=30000"
  "net.ipv4.tcp_rmem=4096 1048576 134217728"
  "net.ipv4.tcp_wmem=4096 1048576 134217728"
  "net.ipv4.udp_rmem_min=16384"
  # SHM 段走 /dev/shm；避免大页回收抖动
  "vm.swappiness=1"
)

for kv in "${SYSCTL[@]}"; do sysctl -w "$kv" >/dev/null && echo "sysctl $kv"; done

if [[ $PERSIST -eq 1 ]]; then
  printf '%s\n' "${SYSCTL[@]}" > /etc/sysctl.d/90-sensorhub.conf
  echo "written /etc/sysctl.d/90-sensorhub.conf"
fi

if [[ -n "$IFACE" ]]; then
  ip link set dev "$IFACE" mtu "$MTU" && echo "$IFACE mtu=$MTU"
  # 加大网卡 ring buffer，减少突发丢包（GigE 6 路 1080p 同时触发会形成微突发）
  if command -v ethtool >/dev/null; then
    max_rx=$(ethtool -g "$IFACE" 2>/dev/null | awk '/Pre-set maximums/{f=1} f&&/^RX:/{print $2; exit}')
    [[ -n "${max_rx:-}" ]] && ethtool -G "$IFACE" rx "$max_rx" && echo "$IFACE rx ring=$max_rx"
    ethtool -C "$IFACE" adaptive-rx off rx-usecs 50 2>/dev/null || true
  fi
  if [[ $PERSIST -eq 1 ]]; then
    echo "提示：MTU/ring 需写入 netplan 或 systemd-networkd 才能重启保留" >&2
  fi
fi

ulimit_l=$(ulimit -l)
echo "当前 shell memlock=$ulimit_l（容器用 ulimits.memlock=-1，原生服务用 LimitMEMLOCK=infinity）"
