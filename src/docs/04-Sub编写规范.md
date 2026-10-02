# 04 Sub 编写规范

参考实现：`sub_client/sub-template-python/sub_example.py`、`sub_client/sub-foxglove-preview/foxglove_preview.py`。

## 1. 三种典型用法

```python
from sensorhub.session import open_session
from sensorhub.sub import subscribe_frames, LatestSlot, SyncAssembler

session = open_session()                     # peer 模式：与本机 pub 直连，SHM 零拷贝

# a) 只处理最新帧（AI 推理慢于采集时自动丢旧帧）
slot = LatestSlot()
subscribe_frames(session, "rig/camera/cam_corner_0/image", slot.put)
while True:
    fr = slot.get(timeout=1)                 # 工作线程
    img = fr.payload()                       # bytes；fr.header 为 FrameHeader

# b) 多传感器同步组（按 trigger_ns 对齐）；required 必须是具体 key（不能含通配符），据此判断"到齐"
required = [f"rig/camera/cam_corner_{i}/image" for i in range(4)] + ["rig/lidar/lidar0/points"]
asm = SyncAssembler(required, on_group, base_hz=10, timeout_s=0.3)   # on_group(slot_ns, {key: Frame})
subscribe_frames(session, "rig/camera/*/image", asm.push, max_hz=10)  # 不在 required 中的 key 自动忽略
subscribe_frames(session, "rig/lidar/lidar0/points", asm.push)

# c) 降频（预览/监控）：max_hz=1 或 0.1 —— 按网格对齐抽帧，多路之间抽到的是同一时刻
subscribe_frames(session, "rig/camera/*/image", on_preview, max_hz=1)
```

## 2. 必须遵守

| # | 规则 | 原因 |
| --- | --- | --- |
| S1 | 订阅回调（zenoh 线程）里**只做入队**，处理放到自己的线程 | 回调阻塞会拖慢该 session 的所有订阅 |
| S2 | 用 `trigger_ns` 对齐多传感器，不要用到达时间 | 不同传感器传输/处理延迟不同 |
| S3 | 要长期持有数据就 `fr.payload()`（拷贝一次）；不要保存 sample 对象本身 | SHM 缓冲由发布方回收，长期持有会耗尽 SHM 池 |
| S4 | 本机程序使用 peer 模式（默认）；不要改为 client | client 模式经路由器转发，失去 SHM（实测 6MB 帧 p50 3.7ms vs 1.1ms） |
| S5 | 只订阅需要的 key；不要订阅 `rig/**` 后自己过滤 | 每个匹配的大数据通道都会传输到本进程 |
| S6 | 长期运行的 sub 也注册一个 `SensorNode("app", name)` 并发布自己的结果通道 | 控制台可见、可监控其频率/延迟 |
| S7 | 降频用 `max_hz`（`Decimator`），不要靠 sleep | 网格对齐抽帧保证多路同时刻 |
| S8 | 检查 `header.seq` 断档 / `Flags.GAP_BEFORE` 处理丢帧 | |
| S9 | 不要向 `status/meta/ctrl/event` 以外的其他实例通道 put 数据 | key 所有权属于发布者 |

## 3. 跨服务器订阅

服务器 D 的程序配置与 A 完全相同（连接本机 `tcp/127.0.0.1:7447`），由 D 的 zenohd 自动向 A 订阅。
注意：跨机没有 SHM，按 10GbE 预算：6 路 Bayer 1080p@20Hz ≈ 2.0 Gbit/s，可行；BGR8 则 ≈ 6 Gbit/s，不推荐。
D 上多个程序订阅同一 key 只占一份 A→D 带宽。

## 4. 数据解码

| encoding | 解码 |
| --- | --- |
| bayer_* | `cv2.cvtColor(np.frombuffer(p, np.uint8).reshape(h, w), cv2.COLOR_BayerRG2BGR)`（注意 OpenCV 命名与 RGGB 的对应关系，见 `foxglove_preview.py`） |
| xyzirt_f32 / xyzi_f32 | `sensorhub.codecs.points_from_bytes(p, header.encoding)` → numpy 结构化数组 |
| imu_v1 | `sensorhub.codecs.imu_array(p)` → numpy 结构化数组（每批 = 一个网格周期内的全部样本） |
| json | `json.loads(p)` |

C++：`sensorhub::parse_header(attachment)`，点结构 `PointXYZIRT`，IMU `ImuSampleV1`。

## 5. Foxglove 预览

`sub-foxglove-preview` 把相机（Bayer→RGB、缩放）、点云（抽稀）、IMU/PLC（JSON）以 `--hz`（默认 1Hz，可 0.1）
转为 Foxglove WebSocket（默认 8765）。它自身也是 `app/foxglove_preview` 实例，`preview_hz`/`max_width`/`max_points`
可在控制台在线修改。**只用于查看**，录制请用 MCAP 录制器（任务 T09）。
