# 多传感器 Zenoh 采集平台设计

## 1. 目标与结论

目标是在服务器 A 独占采集 6 路 GigE 相机、2 路雷达、1 路 IMU，并让 A、D 上的多个容器低延迟消费数据；支持 PLC 等新数据源、Web 管理、配置版本、Foxglove 低频预览和 AI/录制程序。

推荐采用“采集适配器 + Zenoh 数据总线 + 平台控制面”的三层架构：

```text
传感器 ──10GbE── 服务器 A
                  ├─ 相机/雷达/IMU/PLC Publisher（每类独占设备）
                  ├─ zenohd-a（路由，不保存原始帧）
                  ├─ Console（目录、指标、配置、审计）
                  └─ 本机 Subscribers（Zenoh SHM）
                         │
                         └────10GbE──── zenohd-d ── 服务器 D Subscribers
```

关键结论：

1. **Zenoh 负责发现、路由和统一 API，不是共享内存变量管理器。** 同机零拷贝需要 Zenoh SHM 能力和显式验证；跨 A/D 必然经过网络复制。
2. **原始大数据不进入数据库或 REST Storage。** Web 面板读取 Publisher 的目录、状态和指标；备注、期望配置及版本保存在 SQLite/YAML 历史中。
3. **A 是传感器唯一所有者。** D 默认只消费 Zenoh 数据，不能同时打开相机 SDK 或绑定相同雷达 UDP 端口。
4. **同步分三级。** 优先 PTP/PPS + 硬件触发；其次设备时间同步；最后才是软件时间戳对齐。多个容器使用相同 10/20 Hz 定时器不等于同步采集。
5. **先冻结契约并压测，再迁移驱动。** 6 路 1080p RGB8、20 Hz 的裸数据约为 746 MiB/s（约 6.0 Gbit/s，未计协议开销），同时转发到 D 会逼近万兆链路的工程上限。

## 2. 能力边界

### 2.0 现有仓库的迁移前置问题

现有 `resource/` 是重要参考，但不是一套可直接启动的完整生产基线。任务 00 开始前必须登记并解决或隔离以下问题：

| 问题 | 当前状态 | 处理要求 |
|---|---|---|
| Zenoh 实现 | 仓库中没有 Zenoh 依赖或运行代码 | 先做 Common SDK 与基准，不能直接修改所有驱动 |
| 生产频率 | `template_prefix_full.yaml` 为 `hz: 2`，现有 `validate_hz()` 只允许 5/10/20/25/40 | 配置 schema 统一合法范围；禁止静默改值 |
| 配置挂载 | Compose 引用 `../configs/${SENSORS_CONFIG}`，对应目录/文件未入库 | 补齐可运行样例和启动前校验 |
| GigE 真机驱动 | 仓库 `driver.py` 是灰帧桩，真实 SDK 代码仅可能存在于 base image | 从固定 digest 镜像提取/确认源码与许可证，真机验收不能使用桩 |
| 隐式依赖 | `sensors_lib.py`、`gige_sync.shm.ring`、`gige_sync.clients.topics` 等不在仓库 | 建立依赖清单；不得依靠未版本化的镜像内部模块 |
| SHM 命名 | `gige_` 与 `camera_` 两套默认值并存 | 在数据契约中选定唯一 canonical 名称，bridge 显式映射 |
| SHM 协议 | Publisher 与 Subscriber 存在重复且有差异的 ring 实现 | 先做 golden fixture 兼容测试，再迁移 |
| 镜像版本 | Compose `.env` 使用 v7.1.0，部分 Dockerfile 仍引用 v6.x | 固定 digest 并输出兼容矩阵 |
| 设备配置 | lidar1 enabled 状态、IMU UDP/串口方式、Web/Foxglove 端口不一致 | 形成单一 source of truth，环境差异用 overlay 表达 |
| Docker 路径 | README 引用的部分 compose/Dockerfile 路径与仓库实际目录不同 | CI 中增加路径和 Compose config 校验 |

迁移过程中，`resource/` 保持只读参考属性；确认可复现之前，不把基础镜像内部的代码视为已纳入版本控制。

### 2.1 Zenoh SHM

- 同主机 Publisher/Subscriber 可以共享大块 payload，跨主机时自动退化为普通网络传输。
- 不能只靠挂载 `/dev/shm` 宣称零拷贝；Python/C++ 构建特性、IPC namespace、SHM provider、内存锁定额度和实际收到的 buffer 类型都必须测试。
- Router 可参与本机 SHM 路径，但不是内存池的业务管理面。
- Zenoh SHM API 仍可能随版本变化，因此所有节点固定同一经过验证的 Zenoh 版本，不使用 `latest`。
- 如果 Python 驱动无法稳定提供零拷贝，热路径使用 C++ Publisher；Python 只做控制面。

### 2.2 Router 与 Web

`zenohd` 的 REST/Admin Space 能显示 Router/插件状态和访问 key space，但不会自动提供“所有活跃 Topic、频率、备注、配置历史”的完整产品面板。必须由自建 Console 聚合以下信息：

- Publisher 定期发布的 catalog/heartbeat；
- Prometheus 指标；
- Console SQLite 中的备注和配置修订；
- Router Admin Space 状态；
- 交换机 SNMP 或采集 SDK 的链路统计。

### 2.3 同步采集

同步质量按以下等级标记，不能混称：

| 等级 | 实现 | 可声称的结果 |
|---|---|---|
| L0 | 各设备自由运行，主机收到后打时间戳 | 仅近似配对 |
| L1 | 主机 CLOCK_TAI/PTP 对时，按时间窗口配对 | 同一时基、非同一曝光/扫描时刻 |
| L2 | 设备支持 PTP/PPS，使用设备时间戳 | 设备时钟同步 |
| L3 | 相机 Trigger、雷达/IMU 外部事件由同一硬件源触发 | 真正的采样触发同步 |

现有 `sync_grid` 只能做 L1 调度/分组。当前 GigE worker 先抓取 `pending` 帧、到网格点后再发布并填入计划时间，可能把旧帧标成同步帧；迁移时必须改为设备时间戳和实际触发序号。

## 3. 数据面设计

### 3.1 Key expression

禁止以容器名或 IP 作为稳定业务标识。统一使用：

```text
rt/{site}/sensor/{kind}/{sensor_id}/data
rt/{site}/sensor/{kind}/{sensor_id}/status
rt/{site}/sensor/{kind}/{sensor_id}/metrics
rt/{site}/sensor/{kind}/{sensor_id}/schema
rt/{site}/sensor/{kind}/{sensor_id}/control
rt/{site}/sensor/{kind}/{sensor_id}/control/reply/{request_id}
rt/{site}/platform/catalog/{node_id}
rt/{site}/platform/time/status
```

- `site`：部署点，如 `yard01`。
- `kind`：`camera | lidar | imu | plc`。
- `sensor_id`：稳定名称，如 `cam_corner_0`，与序列号/MAC 的映射保存在配置中。
- 数据 key 不带主机名，使 A/D 故障切换不影响消费者；`source_host` 放在消息头。
- 禁止以 `/` 开头，避免同一系统出现两套命名。

### 3.2 通用消息头

每个原始 payload 都附带固定版本的 metadata：

```yaml
schema_version: 1
message_type: camera.image.raw
sensor_id: cam_corner_0
source_host: server-a
source_instance: uuid
sequence: 123456
capture_time_tai_ns: 1790900000000000000
receive_time_tai_ns: 1790900000001200000
publish_time_tai_ns: 1790900000001500000
trigger_id: 9876
clock_domain: ptp
sync_level: L3
payload_size: 6220800
encoding: bgr8
config_revision: sha256:...
```

相机补充 width/height/stride/pixel format；点云补充 point layout/count/frame_id；IMU/PLC 使用有版本的 Protobuf 或固定二进制 schema。头部放 Zenoh attachment 或紧凑 envelope，禁止每帧用巨大 JSON。

### 3.3 QoS

| 数据 | Reliability | Congestion | 原则 |
|---|---|---|---|
| 相机/点云实时流 | best-effort | drop | 消费者慢时保留最新数据，不阻塞采集 |
| IMU/PLC 状态 | reliable | block（有界） | 不可无界积压 |
| control/config | reliable | block | 必须有 request id、超时和响应 |
| heartbeat/metrics | best-effort | drop | 下一个周期会覆盖 |
| MCAP 录制 | 独立订阅策略 | 有界队列 | 过载时报警并记录 drop |

所有 Subscriber 都要有有界队列；实时 AI 默认 `latest-only`。录制可靠性不能通过阻塞相机采集线程实现。

### 3.4 容量基线

```text
RGB8: 1920 × 1080 × 3 × 20 × 6 ≈ 746 MiB/s ≈ 5.97 Gbit/s
Mono8: 1920 × 1080 × 1 × 20 × 6 ≈ 249 MiB/s ≈ 1.99 Gbit/s
```

还需叠加两路点云、封包、重传和 A→D 流量。必须支持以下策略：

- D 只订阅需要的 sensor prefix；
- AI 允许订阅 Mono8、ROI 或派生数据；
- 预览单独降采样/压缩，绝不让原始流经过 Foxglove；
- 每条链路设置带宽告警，压测以 70% 持续链路利用率作为首轮安全线；
- 未经实测，不承诺 6 路 RGB8 20 Hz 与全部雷达原始流可同时无损跨机。

## 4. 控制面与配置

### 4.1 Console 组成

```text
console-api
├─ Catalog consumer：活跃 Publisher、schema、频率、最后更新时间
├─ Metrics adapter：Prometheus/Zenoh metrics
├─ Config service：校验、版本、diff、回滚、应用状态
├─ Control proxy：向设备 Publisher 发送命令
└─ SQLite：备注、用户配置、审计、版本索引

console-web
├─ Topic/传感器目录
├─ 实时 Hz、带宽、丢帧、延迟、同步等级
├─ 网络/设备健康
├─ 配置编辑、校验、diff、回滚
└─ 有权限的动态控制
```

Topic 不靠抓取流量“猜测”。每个 Publisher 在启动和每 2 秒发布 catalog heartbeat，包含 key、schema、备注默认值、期望 Hz、实际 Hz、payload 大小、进程/容器实例和配置 revision。Console 以 TTL 标记在线/离线。

### 4.2 配置生命周期

1. Web 修改产生 **draft**，服务端按 JSON Schema 校验。
2. 保存为不可变 revision：`config-history/<timestamp>-<sha>.yaml`。
3. 用户选择 Apply，Console 给相关 Publisher 发送 revision 与预期旧 revision。
4. Publisher 验证设备能力并返回 `accepted / rejected / restart_required`。
5. 成功后原子更新 `active.yaml`；失败保持旧配置并记录原因。
6. 回滚也创建新 revision，不直接覆盖历史。

备注与传感器配置分开存储。备注更新不应触发相机重启。数据库和配置目录使用持久卷，并提供导出/恢复。

### 4.3 动态参数

| 参数 | 应用方式 |
|---|---|
| 曝光、增益、Gamma | SDK 支持时在线应用，读回确认 |
| 发布/预览频率 | 在线应用；采集频率与下采样频率分开 |
| 设备触发频率 | 受硬件 Trigger/PTP 和曝光上限约束 |
| RGB/Mono、分辨率、ROI | `restart_required`；重建 SHM pool/schema |
| 传感器 IP/MAC/端口 | 维护模式应用，避免采集时抢占 |

控制命令必须带 `request_id`、操作者、目标 revision、超时和幂等键。Web 不直接调用厂商 SDK。

## 5. GigE 网络监控

`gige_discover.py` 使用 Aravis 发现并打开 GenICam 控制通道。它可能与已持有设备控制权的厂商 SDK 冲突，也可能在 6 路高负载采集时增加广播/控制流量，因此不能作为高频旁路探针。

推荐顺序：

1. **受管交换机 SNMP/Telemetry**：读取相机所在端口的 `ifHighSpeed`、up/down、错误包、丢包；这是物理端口协商速率最可靠的来源。
2. **采集进程内读取 GenICam 节点**：复用已经打开的 SDK handle，低频读取 `GevLinkSpeed`、包重传、丢帧，避免第二进程抢占。
3. **被动主机指标**：NIC link、RX drops、socket drops、每帧 receive/capture timestamp。
4. `gige_discover.py` 仅用于启动前、离线或手工维护扫描。

“相机网络延迟”要拆成：

- `receive_time - capture_time`（设备时钟已同步时有效）；
- Trigger 到帧完成时间；
- 丢包/重传数；
- 帧间抖动；
- 最后帧 age。

ICMP ping 只能反映控制面往返时间，不能代表图像传输延迟。

## 6. 部署

### 6.1 Router

提供两种等价部署：

- Docker Compose：固定镜像 digest/version、host network、健康检查、只读配置、持久日志；
- systemd：同一份 `zenohd.json5`，使用受限用户、自动重启、资源限制。

A/D 各部署一个 Router，显式配置监听与互连 endpoint。不要依赖 multicast 跨 Docker 网络自动发现。仅在管理网暴露 REST，生产数据端口用防火墙和 Zenoh ACL/TLS 限制。

### 6.2 Publisher 容器

- 传感器容器通常使用 `network_mode: host`，绑定明确 NIC/IP。
- 需要 SHM 的容器加入兼容 IPC namespace，并设置 `/dev/shm`、`memlock`、用户/组权限；最终选项以基准测试为准。
- SDK base image 可以复用现有 v7.1.0 镜像，但业务镜像必须固定 digest，overlay 只加入应用与 Zenoh 运行库。
- 每个传感器只允许一个主采集实例；使用文件锁/设备租约防止 Compose 重复启动。
- CPU affinity、NUMA、IRQ affinity、socket buffer 和 jumbo frame 作为压测后优化项，不能在所有网卡不一致时盲目启用。

### 6.3 故障模式

- Router 重启：采集继续，Publisher 自动重连；本地有界队列不得无限增长。
- Console 故障：数据面不受影响，动态配置暂不可用。
- D 断链：A 不因 D 的可靠订阅而阻塞。
- Publisher 崩溃：catalog TTL 标离线，systemd/Compose 拉起；sequence/source_instance 可识别重启。
- 配置失败：保持 last-known-good；自动应用不能跨越 `restart_required`。

## 7. Publisher / Subscriber 示例规范

以下是接口形态，不锁定具体 API 拼写；`03-common-sdk` 任务根据固定 Zenoh 版本提供可运行实现。

```python
# Publisher：采集线程获得真实设备时间戳后交给有界发布队列
publisher = SensorPublisher(
    key="rt/yard01/sensor/camera/cam_corner_0/data",
    schema="camera.image.raw/v1",
    queue="latest-only",
    qos="realtime-drop",
    shared_memory=True,
)
publisher.publish(payload, metadata={
    "sequence": sequence,
    "capture_time_tai_ns": device_timestamp,
    "receive_time_tai_ns": tai_now_ns(),
    "trigger_id": trigger_id,
    "encoding": "bgr8",
})
```

```python
# AI Subscriber：回调不做推理，只把最新 buffer 交给 worker
subscriber = SensorSubscriber(
    key_expr="rt/yard01/sensor/camera/*/data",
    queue="latest-only",
    require_schema="camera.image.raw/v1",
)
for sample in subscriber:
    validate_metadata(sample)
    inference_worker.replace_latest(sample)
```

```python
# Foxglove：原始流在入口限频，默认 1 Hz；可配 0.1 Hz
preview = RateLimitedSubscriber(
    key_expr="rt/yard01/sensor/**/data",
    max_hz_by_kind={"camera": 1.0, "lidar": 1.0, "imu": 10.0},
)
```

统一要求：

- 回调中禁止磁盘 IO、编码或 AI 推理；
- payload schema 与 metadata schema 分别版本化；
- 指标至少包含 rx/tx Hz、bytes、drop、queue depth、处理延迟；
- 日志含 sensor_id、source_instance、sequence、revision；
- SIGTERM 优雅关闭，先停采集，再 drain/撤销 Publisher；
- mock 数据必须走与真机相同的数据路径。

## 8. 实施顺序

```text
00 契约与基准
 ├─ 01 Router 部署
 ├─ 02 Web Console
 └─ 03 Common SDK
      ├─ 04 GigE Publisher
      ├─ 05 LiDAR Publisher
      ├─ 06 IMU/PLC Publisher
      ├─ 07 同步与时钟
      ├─ 08 Foxglove Subscriber
      └─ 09 录制/AI Subscriber
           ├─ 10 可观测与网络
           └─ 11 集成发布
```

各任务的可委派说明、输入资源和验收条件位于 `tasks/zenoh-migration/`。

## 9. 迁移策略

采用双轨迁移，不一次删除原 SHM：

1. Common SDK 先用 mock 验证 Zenoh SHM 与跨机路径。
2. 为旧 SHM 写临时 bridge，旧 Publisher 不动，让新 Subscriber 提前接入。
3. 逐类替换 Publisher：IMU/PLC → LiDAR → GigE。
4. 对比旧/新路径 sequence、时间戳、吞吐和录制结果。
5. 所有消费者迁移后停用 bridge，保留一个版本回退窗口。

临时 bridge 只用于迁移，不作为永久架构，否则会增加一次复制并产生双重 ownership。

## 10. 完成定义

- 同机实测证明大 payload 使用 SHM；跨机吞吐和 CPU 有报告。
- 6 相机 + 2 雷达 + 1 IMU mock soak 24 小时，无无界内存增长。
- 真机的实际时间戳误差、丢帧、重传、带宽有基线。
- Console 能显示在线状态、实际 Hz、带宽、延迟、备注和配置 revision。
- 配置支持校验、diff、应用失败保留旧配置、回滚。
- Foxglove 限频不影响原始采集频率。
- A/D 断链、Router/Console 重启、单设备掉线均有自动化故障测试。
- 所有镜像和 Zenoh 依赖固定版本，部署、升级和回滚文档齐全。
