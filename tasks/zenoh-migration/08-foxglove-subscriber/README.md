# 任务 08：Foxglove 限频预览 Subscriber

## 目的

将 Zenoh 原始流转换为 Foxglove WebSocket，用低频预览检查效果，同时保证预览客户端不会影响采集数据面。

## 要求

- 入口先限频再做 JPEG/点云转换：相机默认 1 Hz，可配置 0.1～5 Hz；IMU 可独立配置。
- 每个 sensor 独立 token bucket/latest-only，慢浏览器不反压 Zenoh Publisher。
- 相机支持缩放和 JPEG 质量；点云支持抽样/体素降采样。
- 保留 capture timestamp、frame_id 和 schema 映射。
- WebSocket 客户端数量、编码耗时、丢预览帧、输出带宽有指标。
- 配置变更不重启原始 Publisher。
- Foxglove 未连接时停止昂贵编码，仍可维持轻量状态订阅。

## 可用资源

- `resource/sub_client/multimodal_src/multimodal_common/clients/foxglove_bridge.py`
- `resource/pub_server/imu-sync/imu_sync/clients/foxglove_client.py`
- `resource/pub_server/lidar-sync/lidar_sync/clients/foxglove_client.py`
- base image：`client-foxglove-v7.1.0-x64`。

## 交付物

- `src/sub_client/foxglove/`
- Dockerfile/Compose、配置 schema、Foxglove layout。
- 相机/点云/IMU/PLC 映射文档。

## 测试与验收

- 原始 20 Hz 输入、1 Hz 预览时，输出频率在容差内且 Publisher drop 不增加。
- 10 秒更新一次（0.1 Hz）配置有效。
- 断开所有浏览器后编码 CPU 明显下降。
- 慢客户端、多个客户端、坏 payload、schema 切换测试不导致内存增长。
- Foxglove 可正确显示 6 相机、2 点云及 IMU 时间轴。

## 非目标

Foxglove 不是无损记录通道，也不承诺显示原始采集频率的所有帧。
