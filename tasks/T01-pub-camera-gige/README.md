# T01 GigE 相机采集 pub（海康 MVS）

## 目的

用 Zenoh 替换 `resource/pub_server/gige-camera-sync` 的 SHM 输出：6 路 1080p GigE 相机按全局网格触发同步采集，
发布到 `rig/camera/{name}/image`；支持在控制台在线改频率（10→5→1Hz）、RGB/Mono、曝光；
在采集进程内部读取链路协商速率（**不再使用与采集冲突的 `gige_discover.py`**）。

## 范围

做：
* 按 `sensors.yaml` 的 `cameras` 打开设备（按 serial 匹配，其次 mac），一个进程管理全部本机相机，每路一个采集线程。
* 触发模式 `trigger_mode`：`software`（必须）、`action_command`（PTP，尽量）、`hardware`（外触发线，配置即可）、`free_run`（调试）。
* 动态参数（见接口约定），频率变更在下一个整秒生效。
* 掉线重连 / 设备不在（`absent`）/ 被占用（`occupation_retry_sec`）处理，进程不退出。
* 链路监控：每 `link_check_interval_sec` 用自己的句柄读 `GevLinkSpeed`，测量控制通道 RTT。
* 可选 JPEG `preview` 通道（`preview.enabled`，限频 `preview.hz`）。

不做：图像处理/去畸变；录制（T09）；网段扫描（T06）。

## 可用资源

* 旧代码：`resource/pub_server/gige-camera-sync/gige_sync/driver.py`（MVS 封装：打开、参数、取图）、`worker.py`（网格循环）、
  `discovery.py`（GVCP 发现）、`scripts/gige_arv_util.py`（参数名参考）。
* 镜像：`GIGE_IMAGE=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:server-gige-sync-v7.1.0-x64`（含 MVS SDK，`/opt/MVS`）。
* SDK：`SensorNode`、`GridTimer`、`image_header`、`ParamSpec`、`ShmPool`；参考实现 `src/pub_server/pub-mock/pub_mock.py` 的 `MockCamera`
  （参数表、`_on_set`、`set_device` 字段与本任务完全一致，可直接照搬结构）。
* 文档：`src/docs/03`（规则 P1–P13）、`05`（触发方式与 MVS Action Command 用法）、`07`（链路监控 L1）。

## 接口约定

* key：`rig/camera/{name}/image`（可选 `rig/camera/{name}/preview`，encoding=jpeg）
* encoding：由 `publish_encoding` 决定，默认 `bayer_rggb8`（按相机实际 Bayer 排列选 rggb/bggr/gbrg/grbg）；`mono8`；`bgr8`（pub 内转换，仅调试）
* 帧头：`trigger_ns`=网格时刻；`stamp_ns`=曝光开始时刻（PTP 模式用相机时间戳换算，flags 加 `CLOCK_PTP`；否则用主机取图时刻减去曝光/传输估计，flags 加 `CLOCK_HOST`）；
  flags 标明 `SYNC_SOFT_TRIGGER`/`SYNC_ACTION_CMD`/`SYNC_HW_TRIGGER`
* 参数（ParamSpec）：

| name | type | live | 说明 |
| --- | --- | --- | --- |
| hz | enum [1,2,5,10,20,25] | ✓ | 网格频率，下一个整秒生效，返回 `effective_ns` |
| trigger_mode | enum | ✗(内部停启流) | software / action_command / hardware / free_run |
| pixel_format | enum [BayerRG8, Mono8, RGB8] | ✗(内部停启流) | 相机输出；RGB8 时提示带宽 |
| publish_encoding | enum | ✓ | bayer_* / mono8 / bgr8 |
| exposure_auto | enum [Off, Once, Continuous] | ✓ | |
| exposure_us | int 20–100000 | ✓ | `exposure_auto=Off` 时生效 |
| gain_auto / gain_db | enum / float | ✓ | |
| packet_size / packet_delay | int | ✗ | GevSCPSPacketSize / GevSCPD |
| width / height | int | readonly | |

* 自定义 op：`reconnect`、`trigger_once`（立即抓一帧）、`read_feature{name}`（只读 GenICam 节点，调试用）
* `status.device`：`ip mac serial model firmware link_speed_mbps expected_link_mbps ctrl_rtt_ms temperature_c
  stream_packets_lost stream_resend` ；链路降速 → `state=degraded`，`reason="link 100Mbps"`，并 `emit_event("warn")`

## 交付物

```
src/pub_server/pub-camera-gige/
  README.md  Dockerfile  pub_camera_gige.py（或 C++）  mvs_camera.py（设备层）  tests/
```

## 测试与验收

单元（无硬件，CI）：设备层抽象可替换为 fake，测试参数校验、`set_params` 流程、频率切换在整秒、断线重连状态机、`absent` 状态。

硬件：
1. 6 路 BayerRG8 1080p@20Hz 运行 30 分钟：每路 `rate_hz=20±0.1`，`gaps=0`，CPU 占用记录在 PR。
2. 软件触发：同一 `trigger_ns` 下 6 路 `stamp_ns` 跨度 p95 < 2ms；Action Command（若相机支持）p95 < 0.1ms。
3. 控制台改 hz 20→10→5→1→20、Bayer↔Mono、曝光 2000↔20000us：均无需重启进程，生效时刻与 `effective_ns` 一致，事件页有记录。
4. 拔掉一根网线 → 该路 `error`/`degraded` 且其它路不受影响；插回 5s 内恢复 `running`。
5. 把一台相机接到百兆口 → 控制台显示 100Mbps 且标红；**采集过程中**链路检测不导致任何丢帧。
6. `sub-template-python --mode stats` 在同机测得传输延迟 p50 < 2ms（SHM），在服务器 D 测得 < 10ms。
