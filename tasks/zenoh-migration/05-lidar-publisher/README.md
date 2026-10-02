# 任务 05：LiDAR Publisher

## 目的

把两路 RoboSense 雷达的 rs_driver 数据转成有版本的 Zenoh 点云流，保留设备标定、真实时间戳和故障隔离。

## 要求

- C++ rs_driver 直接发布，避免 C++→旧 SHM→Python→Zenoh 的永久双重复制。
- 两台雷达使用独立 MSOP/DIFOP 目的端口或可证明安全的 socket 分流。
- 发布点字段布局、frame_id、点数、回波类型、device/receive timestamp 和 packet loss。
- 支持 UDP socket buffer、包乱序/丢失指标、自动重连和 PCAP 回放。
- 设备支持 PTP/PPS/GPS/外部触发时接入任务 07；不支持时标记 L0/L1，不伪造 trigger id。
- 一个雷达异常不阻塞另一个 Publisher。
- 控制命令仅开放 SDK 明确支持且可读回确认的参数。

## 可用资源

- `resource/pub_server/lidar-sync/`
- `resource/pub_server/lidar-sync/rs_driver/`
- `resource/pub_server/lidar-sync/cpp/lidar_capture_main.cpp`
- base image：`server-lidar-sync-v7.1.0-x64`（实施时固定 digest）。

## 交付物

- `src/pub_server/lidar/`
- rs_driver→Common SDK adapter、PCAP/mock publisher。
- 双雷达配置与 Docker Compose。
- 点云 schema、设备能力和网络端口说明。

## 测试与验收

- PCAP 回放 checksum/点数与参考结果一致。
- 两路并发 10 Hz，慢 Subscriber 不阻塞 UDP 接收线程。
- 注入丢包、乱序、DIFOP 缺失、单雷达断流，指标和恢复符合预期。
- Foxglove/MCAP consumer 可按 schema 正确解码。
- 真机测试记录设备时间来源和同步等级。

## 非目标

不通过软件 sleep 制造“雷达 10 Hz 硬件触发”；SDK/型号不支持的能力必须明确列出。
