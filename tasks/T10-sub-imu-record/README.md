# T10 IMU 记录（迁移 client-imu-record）

## 目的

把 `resource/sub_client/client-imu-record-src` 改为订阅 `rig/imu/*/data`，保持原有文件格式与轮转策略，下游工具无需改动。

## 范围

* 订阅 IMU 批数据，解包为样本（`sensorhub.codecs.imu_array`），按旧格式写文件（`writer.py`）、按旧策略轮转（`rotate.py`）。
* 样本按 `stamp_ns` 单调写入；检测重复/乱序/断档并计数。
* 注册 `SensorNode("app", "imu_record")`，status 中给出当前文件、写入速率、丢样数。

## 可用资源

* 旧代码：`imu_record/{main,recorder,writer,rotate,config}.py`、`tests/`。
* 数据源：pub-mock 的 `MockImu`（无硬件）或 T03。

## 交付物

`src/sub_client/sub-imu-record/`：README.md、Dockerfile、imu_record/、tests/（迁移旧测试 + 新增 Zenoh 输入测试）

## 测试与验收

1. 旧测试全部通过；同一输入（mock 回放）下新旧程序输出文件逐字节或逐字段一致（说明差异）。
2. pub-mock 运行 1 小时：样本无丢失（与 pub 端 count 一致），轮转文件数量与时间正确。
