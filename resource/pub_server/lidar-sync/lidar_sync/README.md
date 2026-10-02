# lidar-sync

RoboSense **rs_driver** 原生采集（**无需 ROS1/ROS2**），server/client 通过 `/dev/shm` 共享点云数据。

架构参考 `ref/gige-camera-sync-antifragile/`：

- **Server**：`lidar-capture`（C++ rs_driver）或 mock 模式 → 共享内存
- **Client**：Foxglove WebSocket（默认 `:38765`）、本地 PCD 保存
- **Web 管理**：`:18090` 局域网发现、按 MAC/IP/端口添加雷达、异常日志与自动重连

## 快速开始

```bash
cd lidar-sync
pip install -e ".[all]"

# 编译 C++ 采集端（真机）
cd cpp && chmod +x build.sh && ./build.sh

# mock 自测 5 秒
lidar-sync run -c configs/mock_test.yaml --mock

# 真机 server
lidar-sync server -c configs/server.yaml

# Foxglove 客户端（另开终端）
lidar-sync client foxglove -c configs/client_foxglove.yaml
```

Web 管理：http://\<host\>:18090

## 多雷达

每台雷达在 YAML 中独立配置 `msop_port` / `difop_port`（必须在雷达 Web 里改目的端口，避免冲突）。

## Docker

```bash
# Server
cd docker-server && docker compose up -d --build

# Client（共享 server IPC）
cd docker-clients && docker compose up -d
```

## 环境变量

| 变量 | 说明 |
|------|------|
| `LIDAR_SUBNET` | 发现子网，如 `192.168.1.*` |
| `LIDAR_WEB_PORT` | Web 端口 |
| `LIDAR_DRIVER_BINARY` | `lidar-capture` 路径 |
| `LIDAR_MOCK=1` | 合成点云 |

## MSOP / DIFOP

- MSOP（点云）：默认 6699，包长约 1200
- DIFOP（标定/设备信息）：默认 7788，包长 256

采集由 rs_driver 完成（通道排列、垂直角、时间戳、DIFOP 标定表），不经过 ROS。
