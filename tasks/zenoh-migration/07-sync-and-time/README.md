# 任务 07：同步触发与统一时间

## 目的

建立可以量化的跨传感器时间基准，区分硬件触发、设备时钟同步和软件配对。

## 要求

- 服务器 A/D 使用 PTP（优先硬件 timestamp）同步到同一 grandmaster，应用统一读取 CLOCK_TAI。
- 调研每个准确型号的相机、雷达、IMU 是否支持 PTP、PPS、Trigger in/out，并形成接线矩阵。
- 相机使用同一硬件 trigger fanout；雷达/IMU 只有型号支持时才接入同源信号。
- 定义 trigger id 生成、跨进程分发、丢 trigger 检测和启动 epoch。
- 软件 synchronizer 按 capture timestamp 匹配，输出误差与缺席设备，不改写原始时间戳。
- 提供 time status Publisher：offset、jitter、GM id、lock state、leap/TAI 信息。
- PTP 未锁定或设备时钟跳变时，数据降级 sync level 并告警。

## 可用资源

- `resource/sub_client/multimodal_src/multimodal_common/sync_grid.py`
- `resource/pub_server/imu-sync/imu_sync/sync_grid.py`
- `resource/template_prefix_full.yaml` 中 `grid_orchestrator` 仅作软件网格参考。
- 各厂商硬件手册与实际设备型号是本任务必要输入。

## 交付物

- `src/platform/time-sync/` 的 ptp4l/phc2sys 配置与状态 exporter。
- 硬件接线/能力矩阵。
- trigger protocol、同步分组库和测试工具。
- L0～L3 判定标准与运维手册。

## 测试与验收

- 注入 PTP unlock、时钟跳变、trigger 丢失，等级和告警正确。
- 使用 LED/脉冲/示波器或设备诊断做真机端到端测量，不只比较软件日志。
- 报告每类设备 capture time 差值的 p50/p95/p99/max。
- 不支持硬件触发的设备被明确标为 L0/L1/L2，不允许测试通过后写成 L3。
- 软件配对在某路缺帧时不会错配下一周期帧。

## 非目标

容器调度、共享的 10/20 Hz sleep 或相同 publish timestamp 都不算硬件同步。
