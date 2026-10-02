# imu-sync

Yesense IMU 采集与共享内存分发系统，架构仿照 `lidar-sync`。

## 功能

- **Server**：按配置频率（默认 20Hz，网格对齐）采集 IMU 数据写入 POSIX 共享内存
- **Client**：只读附着共享内存，广播到 Foxglove WebSocket（默认 `28765`）或录制 MCAP
- **Web 管理**：`http://<host>:28090` 自动发现、添加/移除 IMU、异常日志
- **发现**：串口 USB IMU + 局域网 Yesense 协议（`0x59 0x53`）UDP/TCP
- **Foxglove 双通道**：
  - `sensor_msgs/Imu` 等价消息（加速度、角速度、四元数）
  - `SceneUpdate` 3D 位姿（吊具 xyz + 方向角实时显示）

## 快速开始

```bash
cd imu-sync
pip install -e ".[all]"

# Mock 测试（无需硬件）
imu-sync run -c configs/mock_test.yaml

# 生产 Server
IMU_SUBNET=192.168.1.* imu-sync server -c configs/server.yaml

# Foxglove 客户端
imu-sync client foxglove -c configs/client_foxglove.yaml
# Studio 连接 ws://<host>:28765

# MCAP 录制
imu-sync client mcap -c configs/client_mcap.yaml
```

## Docker

```bash
# Server
cd docker-server && IMU_SUBNET=192.168.1.* docker compose up -d --build

# Client（需 server 已运行）
cd docker-clients && docker compose up -d
```

## 配置说明

`configs/server.yaml` 通过 **MAC** 绑定 IMU，IP/串口由自动发现填充。支持：

| 参数 | 说明 |
|------|------|
| `mac` | 设备 MAC（网络）或 USB 伪 MAC |
| `transport` | `serial` / `tcp` / `udp` |
| `serial_port` | 如 `/dev/ttyUSB0` |
| `baudrate` | 默认 460800 |
| `imu_ip` / `port` | 网络 IMU 地址 |

环境变量：通常只需 `IMU_SUBNET=192.168.1.*`；可选 `IMU_LOCAL_IP`、`IMU_INTERFACE` 用于高级覆盖。

## Yesense 协议

解析逻辑基于 `Yesense-Decode-Python3-V2.0/`，已集成到 `imu_sync/protocol/yesense_decoder.py`。
