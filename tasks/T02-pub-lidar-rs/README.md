# T02 速腾雷达采集 pub（C++，rs_driver）

## 目的

替换 `resource/pub_server/lidar-sync`（C++ rs_driver → stdout → Python → SHM 的两级桥接），改为 **单个 C++ 进程**
直接把点云写入 Zenoh（SHM），发布 `rig/lidar/{name}/points`，并尽可能让雷达帧与全局网格对齐。

## 范围

做：
* 基于 T07 的 C++ SDK（未完成前可直接用 zenoh-cpp + `common/cpp/sensorhub/frame_header.hpp`，控制面按 `docs/02` 自行实现）。
* 每个设备一个 rs_driver 实例（`msop_port/difop_port/lidar_type/host_address/group_address`）。
* 编码 `xyzirt_f32`（默认，24B/点）或 `xyzi_f32`；点云直接写入 SHM 缓冲（避免中间拷贝）。
* 同步：
  * 机械式雷达（Helios/Ruby/Bpearl…）：PTP/gPTP 授时 + 相位锁定（`phase_lock_deg`），转速对应 `hz`（600rpm=10Hz）。
  * 固态雷达（当前配置 `RSE1`）：PTP 授时，确认是否支持帧起始对齐/同步触发（查型号手册与 DIFOP 字段）；不支持时按 `nearest_slot` 归属网格并在 status 中给出偏差。
  * `trigger_ns` = 该帧归属的网格时刻；`stamp_ns` = 雷达首点时间（雷达时钟，PTP 时 flags 加 `CLOCK_PTP|SYNC_PHASE_LOCK`）。
* DIFOP 信息（型号、序列号、固件、温度、PTP 状态、转速、相位锁定状态）写入 `status.device`。
* 动态参数：`encoding`、`max_points`/`downsample`（可选）、`phase_lock_deg`（若可通过 DIFOP/网页配置下发则 live，否则只读提示）。

不做：点云算法、录制、网段扫描（T06）。

## 可用资源

* 旧代码：`resource/pub_server/lidar-sync/cpp/lidar_capture_main.cpp`（rs_driver 用法、参数）、`cpp/CMakeLists.txt`、`rs_driver/`（SDK 源码）、
  `lidar_sync/pointcloud_codec.py`（旧点格式）、`lidar_sync/discovery/*`（DIFOP 解析参考）。
* 镜像：`LIDAR_IMAGE=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:server-lidar-sync-v7.1.0-x64`（含 rs_driver 依赖）；
  zenoh-c/zenoh-cpp 1.10.x 需在 Dockerfile 中加入（release 包或源码构建）。
* SDK：`src/common/cpp/sensorhub/frame_header.hpp`（`FrameHeader`、`PointXYZIRT`、`sensor_key`、`nearest_slot`）。
* 参考：`pub-mock` 的 `MockLidar`（字段、参数、status 结构）。

## 接口约定

* key：`rig/lidar/{name}/points`；encoding `xyzirt_f32`(21) / `xyzi_f32`(20)
* 帧头：`width`=点数，`height`=1（无序）或线数，`step`=24/16，`count`=点数
* 点的 `t` 字段 = 相对 `stamp_ns` 的秒偏移（float32）
* `status.device`：`ip model serial firmware rpm ptp_locked phase_lock_ok temperature_c packets_lost`

## 交付物

```
src/pub_server/pub-lidar-rs/
  README.md  Dockerfile  CMakeLists.txt  src/main.cpp  src/rs_source.{hpp,cpp}  tests/
```

## 测试与验收

单元：用 rs_driver 的 pcap 回放（或录制的 UDP 包回放）跑 CI；Python `sensorhub.codecs.points_from_bytes` 能正确解析发布的数据（跨语言一致性测试）。

硬件：
1. 10Hz 运行 30 分钟：`rate_hz=10±0.05`，`gaps=0`，单帧点数稳定。
2. 发布延迟 `latency_ms`（pub_ns-stamp_ns）p95 < 15ms；同机 SHM 订阅传输延迟 p50 < 1ms。
3. PTP 模式下 `sync_err_ms` p95 < 1ms（机械式相位锁定）；固态雷达给出实测偏差分布。
4. 与 T01 相机同时运行，`sub-template-python --mode sync` 同步组完整率 > 99.5%。
5. 拔网线/重启雷达 → `degraded`→恢复 `running`，进程不退出。
