# T03 IMU 采集 pub（Yesense）

## 目的

替换 `resource/pub_server/imu-sync` 的 SHM 输出：读取 Yesense IMU（UDP 或串口），把每个网格周期内的样本打包发布到
`rig/imu/{name}/data`，供融合程序按网格时刻对齐。

## 范围

做：
* 传输 `udp`（`port`/`imu_src_port`）与 `serial`（`serial_device`/`baudrate`），复用旧的 Yesense 解码器。
* 每样本时间戳：设备时间可用时用设备时间 + 偏差估计（与主机时钟线性拟合），否则用主机接收时刻（`CLOCK_HOST`）。
* 按 `publish_hz` 网格：`(slot - period, slot]` 内的样本组成一批，`trigger_ns=slot`，`stamp_ns`=首样本时间。
* 统计：实际采样率、丢样、串口/UDP 错误、设备温度 → `status.device`/`extra`。
* 参数：`publish_hz`（live）、`sample_hz`（只读或下发设备配置）、`transport`（restart_required）。

不做：姿态解算算法、录制（T10）。

## 可用资源

* 旧代码：`resource/pub_server/imu-sync/imu_sync/protocol/yesense_decoder.py`、`imu_codec.py`、`server/worker.py`、`discovery/*`（UDP/串口发现）。
* 镜像：`IMU_IMAGE=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:server-imu-sync-v7.1.0-x64`。
* SDK：`sensorhub.codecs.ImuSample` / `pack_imu`（56B/样本，`IMU_SAMPLE_DTYPE`），参考 `pub-mock` 的 `MockImu`。

## 接口约定

* key：`rig/imu/{name}/data`，encoding `imu_v1`(30)，`width=count=样本数`，`step=56`，flags `CLOCK_HOST` 或 `CLOCK_PTP`
* 样本字段：`stamp_ns ax ay az(m/s²) gx gy gz(rad/s) qw qx qy qz temp_c status`

## 交付物

`src/pub_server/pub-imu/`：README.md、Dockerfile、pub_imu.py、yesense.py（解码，可从旧代码迁移）、tests/

## 测试与验收

单元：用录制的原始字节流（放 tests/data）测试解码、分批边界（样本恰在 slot 上属于哪一批）、丢样统计、时钟拟合。

硬件：
1. 200Hz 采样、20Hz 发布运行 30 分钟：每批 10±1 个样本，`gaps=0`，样本时间单调递增、间隔抖动 p95 < 1ms。
2. 控制台改 `publish_hz` 20→10→1，在整秒切换，批大小随之变化。
3. 拔串口/断网后恢复，进程不退出，状态正确。
