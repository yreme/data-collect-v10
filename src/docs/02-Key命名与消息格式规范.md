# 02 Key 命名与消息格式规范

实现：`common/python/sensorhub/keys.py`、`header.py`、`codecs.py`；C++：`common/cpp/sensorhub/frame_header.hpp`。

## 1. Key 命名

```
{prefix}/{kind}/{name}/{channel}
```

| 段 | 规则 | 例子 |
| --- | --- | --- |
| prefix | 一台设备/一套传感器的命名空间，默认 `rig`（env `SENSORHUB_PREFIX` 或 `system.prefix`） | `rig` |
| kind | `camera` `lidar` `imu` `plc` `gnss` `app` `sync` `net` | `camera` |
| name | `[a-z0-9_]{1,32}`，稳定标识（不要用序列号/IP，换设备时 name 不变） | `cam_corner_0` |
| channel | 数据通道或保留通道 | `image` |

数据通道：`image`、`preview`、`points`、`data`、`state`、`objects`、`links`、`schedule`、`tick`。
保留通道（框架使用，业务不要占用）：`status`、`meta`、`ctrl`、`event`。

其它保留 key：

| key | 说明 |
| --- | --- |
| `{prefix}/alive/{kind}/{name}` | liveliness token |
| `{prefix}/sync/master/schedule` | 同步主节点发布的网格计划（频率、相位、生效时间） |
| `{prefix}/sync/master/tick` | 每个网格时刻的心跳（可选，用于软件触发诊断） |
| `{prefix}/cfg/{file}` | 配置 queryable（控制台） |
| `{prefix}/cfg/changed` | 配置变更通知 |
| `{prefix}/net/netprobe/links` | 网络链路探测结果 |

**别名（"左上相机""左侧雷达"）不进入 key**，只存在 `sensors.yaml` 的 `alias` 与控制台别名库中。
key 必须稳定，否则所有订阅者都要改。

通配订阅示例：`rig/camera/*/image`（全部相机）、`rig/*/*/status`（全部状态）、`rig/**`（全部）。

## 2. 帧头 FrameHeader v1（64 字节，小端，放 attachment）

| off | size | 字段 | 说明 |
| --- | --- | --- | --- |
| 0 | 4 | magic | `SHF1` |
| 4 | 2 | version | 1 |
| 6 | 1 | kind | 1 camera / 2 lidar / 3 imu / 4 plc / 5 gnss / 10 app |
| 7 | 1 | encoding | 见下表 |
| 8 | 8 | seq | 每通道从 0 递增；断档 = 丢帧 |
| 16 | 8 | trigger_ns | 网格时刻（Unix ns）；不参与同步为 0 |
| 24 | 8 | stamp_ns | 传感器时刻（曝光开始 / 扫描起点 / 采样时刻） |
| 32 | 8 | pub_ns | 发布时刻（SDK 自动填） |
| 40 | 4 | width | 图像宽；点云=点数；IMU=样本数 |
| 44 | 4 | height | 图像高；点云=1 或线数 |
| 48 | 4 | step | 图像每行字节；点云/IMU=单元素字节 |
| 52 | 4 | count | 元素数（图像=1） |
| 56 | 4 | flags | 见下表 |
| 60 | 4 | reserved | 0 |

encoding：

| 值 | 名称 | 说明 |
| --- | --- | --- |
| 1 / 2 / 3 | mono8 / bgr8 / rgb8 | |
| 4–7 | bayer_rggb8 / bggr8 / gbrg8 / grbg8 | **相机默认发布 Bayer 原始数据**（1080p 2MB，BGR 为 6MB） |
| 8 | mono16 | |
| 10 / 11 | jpeg / png | 仅用于预览/低带宽通道 |
| 20 | xyzi_f32 | 16B/点 |
| 21 | xyzirt_f32 | 24B/点：x,y,z,intensity(f32) ring(u16) pad(u16) t_off_s(f32) |
| 30 | imu_v1 | 56B/样本，见 `codecs.IMU_SAMPLE_DTYPE` |
| 40 / 41 / 42 | json / cbor / protobuf | 结构化数据（PLC 等） |

flags：bit0 硬件触发、bit1 GigE Action Command、bit2 软件触发、bit3 雷达相位锁定、
bit4 stamp 来自 PTP 设备时钟、bit5 stamp 为主机接收时刻、bit6 本帧前有丢帧、bit7 模拟数据。

**兼容规则**：只允许在 reserved 区或末尾追加字段并升级 version；读取方遇到更高版本报错提示升级 SDK。
Python 与 C++ 两份定义必须同步修改，`common/cpp/tests/test_frame_header.cpp` 做字节级比对。

## 3. 结构化数据

* `status`/`meta`/`event`/`ctrl` 一律 JSON（UTF-8），`zenoh.Encoding.APPLICATION_JSON`。
* PLC 等低频结构化数据：payload 为 JSON（或 CBOR），仍带帧头（encoding=json），以便统一统计频率与延迟。
* 小 JSON 不走 SHM（`add_stream(..., shm=False)`）。

## 4. ctrl 协议

请求：`session.get("{base}/ctrl", payload=json({"op": ..., "params": {...}}))`，应答 `{"ok": bool, "result"|"error": ...}`。

| op | 说明 |
| --- | --- |
| `describe` | 返回 `params`（ParamSpec 列表：name/type/value/label/unit/min/max/step/choices/live/group/help/readonly）与自定义 `ops` |
| `get_params` | 当前参数值 |
| `set_params` | `{"hz": 5, "exposure_us": 8000}`；先校验，再调用 pub 的 `on_set`，返回 `applied`、`restart_required` 及 pub 附加信息（如 `effective_ns`） |
| `status` / `meta` | 同对应通道 |
| 自定义 | 如相机 `reconnect`、`trigger_once`，雷达 `reset` |

## 5. status JSON 主要字段

```json
{"kind":"camera","name":"cam_corner_0","state":"running","reason":"","host":"server-a","pid":12,
 "streams":{"image":{"key":"rig/camera/cam_corner_0/image","rate_hz":20.0,"target_hz":20,"msg_bytes":2073600,
   "bytes_per_s":41472000,"latency_ms":{"p50":8.1,"p95":9.0,"max":11.2},"sync_err_ms":{"p50":0.4,"p95":0.6,"max":0.9},
   "gaps":0,"count":12000,"age_ms":12,"shm":true}},
 "device":{"ip":"192.168.1.101","mac":"…","serial":"…","link_speed_mbps":1000,"expected_link_mbps":1000,"rtt_ms":0.3},
 "params":{"hz":20,"exposure_us":12000},"errors":[],"shm_active":true}
```

`state`：`starting` `running` `degraded`（在跑但有问题，如链路降速）`error` `absent`（配置了但设备不在）`stopped`。
