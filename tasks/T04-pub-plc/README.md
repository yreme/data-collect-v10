# T04 PLC 数据 pub（727R 起重机 UDP）

## 目的

把 `resource/pub_server/plc-data-collect` 的 UDP 解析结果发布到 Zenoh（`rig/plc/{name}/state`），
作为"新增数据源只需写一个 pub"的样板，替代旧的 PLC SHM 环形缓冲。

## 范围

做：
* 监听 `udp_port`，复用 `udp_parser.py` 解析为字段字典；按原始频率发布 JSON（encoding=json，带帧头，`CLOCK_HOST`）。
* 连接健康：`health_timeout_sec` 内无包 → `degraded`；解析异常计数；发送端 IP 写入 `status.device`。
* 可选：`publish_hz` 网格发布"最新值"通道 `rig/plc/{name}/state_grid`（便于与相机对齐）。
* 字段说明（单位、含义）通过 `meta` 发布（控制台/Foxglove 可显示）。

不做：Foxglove 场景（交给 T12）、录制（T09）。

## 可用资源

* 旧代码：`resource/pub_server/plc-data-collect/python/udp_parser.py`、`connection_health.py`、`mock_sender.py`（无硬件测试源）、`使用说明.md`。
* 参考：`pub-mock` 的 `MockPlc`。

## 接口约定

* key：`rig/plc/{name}/state`，payload `{"field": value, ...}`（扁平 JSON，数值保持原始单位，单位放 meta）
* meta：`{"fields": {"hoist_height": {"unit": "m", "label": "起升高度"}, ...}}`

## 交付物

`src/pub_server/pub-plc/`：README.md、Dockerfile、pub_plc.py、tests/

## 测试与验收

1. 用 `mock_sender.py` 发送，订阅端收到的字段与 `udp_parser` 解析结果逐字段一致（单元测试）。
2. 停止发送 → `health_timeout_sec` 后 `degraded`，恢复后 `running`；控制台事件页有记录。
3. 实机：频率与 PLC 发送频率一致，`gaps=0`。
