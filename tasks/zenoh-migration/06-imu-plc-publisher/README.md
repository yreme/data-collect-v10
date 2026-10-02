# 任务 06：IMU 与 PLC Publisher

## 目的

迁移小数据、高可靠性的 IMU/PLC 数据源，并提供后续增加低带宽传感器的标准模板。

## 要求

- IMU 支持串口/UDP/TCP 现有模式，发布原始值、单位、坐标系、校准状态和设备时间戳。
- PLC 保留原始报文及解析后的结构化 schema，解析异常不能终止采集。
- IMU/PLC 使用可靠、有界 QoS；网络断开时不允许无限缓存。
- 序列号/MAC/端口映射持久化，自动发现只提出候选，不静默覆盖生产配置。
- 设备支持 PPS/event input 时接入任务 07；否则记录 receive timestamp 和 sync level。
- 提供新增小型 Publisher 的脚手架、配置和规范。
- IMU 与 PLC 分成独立镜像/容器，故障互不影响。

## 可用资源

- `resource/pub_server/imu-sync/`
- `resource/pub_server/plc-data-collect/`
- `resource/pub_server/imu-sync/imu_sync/protocol/yesense_decoder.py`
- base image：`server-imu-sync-v7.1.0-x64`。

## 交付物

- `src/pub_server/imu/`
- `src/pub_server/plc/`
- schema、配置、Dockerfile/Compose、mock sender。
- “新增一种低带宽传感器”开发指南。

## 测试与验收

- 协议 golden fixture 解码测试，单位和坐标转换有明确期望值。
- 串口断开、UDP 重复/乱序、坏 checksum、PLC 异常报文测试。
- Router 断开 5 分钟时 RSS 有界；恢复后不发送过期数据洪峰。
- MCAP 录制后回放与输入 sequence/timestamp 一致。
- 真机 8 小时测试报告 Hz、drop、clock drift 和重连次数。

## 非目标

不把 PLC 控制写操作与只读采集默认混在一个权限域；写 PLC 需单独安全评审。
