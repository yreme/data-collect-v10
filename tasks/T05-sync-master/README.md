# T05 同步主节点 sync-master

## 目的

各 pub 已按本地时钟独立计算全局网格（`docs/05`），sync-master 负责**可观测性与协调**，而不是发触发脉冲：
发布当前网格计划、检查时钟同步质量、汇总各传感器同步误差，并提供"一键改全局频率"。它宕机不影响采集。

## 范围

做：
* 以 `SensorNode("sync", "master")` 运行；1Hz 发布 `rig/sync/master/schedule`：
  `{"base_hz", "phase_ns", "effective_ns", "clock": "ptp|chrony", "ptp": {...}, "chrony": {...}}`。
* 时钟健康：读取 `ptp4l`/`phc2sys`（`pmc -u -b 0 'GET TIME_STATUS_NP'`）或 `chronyc tracking` 的偏移；超阈值 → `degraded` + 事件。
* 订阅 `rig/*/*/status`，汇总各传感器 `sync_err_ms` 与"同一 trigger_ns 下的 stamp 跨度"（订阅数据帧头即可，不碰 payload），发布到 `status.extra.sync_report`。
* op `set_global_hz{hz, kinds?}`：校验 `is_valid_hz`，计算下一个整秒 `effective_ns`，向所有（或指定 kind）实例并发 `set_params{hz}`，返回各实例结果。
* 可选 `tick` 通道：每个网格时刻发布一个空帧（诊断软件触发抖动）。
* 可选：输出硬件触发（GPIO/PPS 卡/PLC），在支持的硬件上把网格时刻变成电平脉冲供相机外触发。

不做：PTP 守护进程本身（用 linuxptp，部署文档说明即可）。

## 可用资源

* SDK：`sync_grid`（`period_ns`、`next_second_boundary`）、`node.ctrl_call`、`sub.subscribe_frames`。
* 配置：`sensors.yaml` 的 `sync` 段（`base_hz`、`clock`、`ptp.interface/domain`）。
* 控制台已有 `/api/ctrl/{kind}/{name}` 代理，`set_global_hz` 可直接在"传感器控制"页的 sync/master 中调用。

## 交付物

`src/pub_server/sync-master/`：README.md、Dockerfile、sync_master.py、tests/；`docs/05` 中补充 linuxptp 部署步骤。

## 测试与验收

1. 单元：mock 多个 SensorNode，`set_global_hz` 在同一 `effective_ns` 生效（各实例返回值一致）。
2. 与 pub-mock 联调：同步报告中跨度/误差与 mock 注入的抖动一致。
3. 真机：拔掉 PTP 网线或停 chrony，60s 内出现 `degraded` 与事件；恢复后自动清除。
