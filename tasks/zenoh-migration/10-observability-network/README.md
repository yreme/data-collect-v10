# 任务 10：可观测性与传感器网络健康

## 目的

统一采集链路、Zenoh、主机、交换机和设备指标，并为 Web Console 提供可信的链路速率与延迟来源。

## 要求

- Publisher/Subscriber 暴露 Hz、bytes、sequence gap、queue depth、drop、处理延迟、last sample age。
- Router/主机暴露连接、吞吐、CPU、RSS、NIC drops/errors、socket drops。
- 受管交换机通过 SNMPv3/Telemetry 读取每个传感器端口 speed/duplex/up、CRC/error/drop；维护 MAC→端口映射。
- 采集进程内读取 GigE packet resend/frame drop；禁止旁路进程高频抢占控制权。
- 定义 capture→receive、receive→publish、publish→consume 延迟；时钟未同步时不展示伪精确跨机延迟。
- Prometheus 指标低基数；sensor id 可作 label，sequence/request id 不可作 label。
- 告警覆盖离线、低协商速率、频率下降、带宽过载、PTP unlock、drop 增长和磁盘不足。

## 可用资源

- `resource/pub_server/gige-camera-sync/scripts/gige_discover.py`
- `resource/pub_server/gige-camera-sync/scripts/bandwidth_test.py`
- 任务 01 Router Admin Space、任务 02 Console、任务 07 time status。
- 交换机型号、管理 IP、端口映射和只读 SNMP 凭据需由部署方提供。

## 交付物

- `src/platform/observability/`
- Prometheus scrape、Grafana dashboard、alert rules。
- SNMP exporter 配置和 MAC/端口 inventory。
- Console 指标 API adapter 与指标语义文档。

## 测试与验收

- 拔线、降到 100 Mbps、制造 CRC/drop、停 Publisher 时告警内容和恢复正确。
- `gige_discover.py` 手工扫描与持续采集并发风险有真机记录；生产周期监控不依赖它。
- 高负载下 metrics 开销不超过约定 CPU/带宽预算。
- A/D 时钟不同步时跨机 latency 显示 unavailable 并告警。
- Dashboard 能从端口→设备→Publisher→Subscriber 追踪一条完整路径。

## 非目标

不把主机 NIC 的 10GbE 协商速率误当作每台相机的 1GbE 端口速率，也不把 ICMP ping 当作帧传输延迟。
