# Broker（zenohd 路由器）

每台服务器运行 **一个** zenohd：

| 服务器 | 配置 | 作用 |
|---|---|---|
| A（采集主机） | `config/zenohd.json5` | 本机 peer 的发现中心；对外监听 7447，等待 D 连接；REST 管理口 8000 |
| D（AI 主机） | `config/zenohd.server-d.json5` | 连接 A 的 7447；D 上程序只连本机路由器 |

> 路由器 **不承载本机大流量**：本机 pub/sub 都是 peer 模式，经 gossip 互相发现后直接建立
> 127.0.0.1 连接，图像/点云走 SHM 零拷贝。实测（本仓库 VM，6.2MB 帧）：
> peer↔peer SHM 延迟 p50 ≈ 1.1ms；经路由器的 client 模式 ≈ 3.7ms 且 **不是 SHM**。

## 部署方式 1：Docker

```bash
cd src/broker
docker compose up -d
# 服务器 D：
ZENOHD_CONFIG=zenohd.server-d.json5 docker compose up -d
```

## 部署方式 2：原生（不依赖 Docker）

```bash
cd src/broker/native
sudo ./install.sh                                  # 在线下载 zenoh 1.10.1 standalone
sudo ./install.sh --offline zenoh-1.10.1-x86_64-unknown-linux-gnu-standalone.zip
sudo ./install.sh --config zenohd.server-d.json5   # 服务器 D

systemctl status sensorhub-zenohd
journalctl -u sensorhub-zenohd -f
```

调试时前台运行：`./native/run.sh`。

## 验证

```bash
curl -s 'http://127.0.0.1:8000/@/*/router' | python3 -m json.tool   # 路由器、会话、插件
curl -s 'http://127.0.0.1:8000/rig/*/*/status'                       # 所有传感器心跳（JSON）
curl -s 'http://127.0.0.1:8000/rig/*/*/meta'                         # 所有传感器静态描述
```

## 必须注意

1. **memlock**：Zenoh SHM 会 `mlock` 共享内存段。默认 `ulimit -l` = 8MB 时，SHM 发布会**静默丢失**
   （订阅端收不到，本仓库已复现）。Docker 用 `ulimits: memlock: -1`，systemd 用 `LimitMEMLOCK=infinity`。
2. **网络模式**：所有容器 `network_mode: host`。peer 只监听 `127.0.0.1:0`，跨 docker bridge 网络无法直连，
   会退化为经路由器转发（非 SHM）。
3. **组播发现关闭**：拓扑完全由配置决定。新增服务器只需在其路由器配置 `connect` 指向 A。
4. **REST 插件** 仅用于调试/控制台读取；不要通过 REST 拉取图像原始流。
