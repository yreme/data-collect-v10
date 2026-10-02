# 任务 00：数据契约、版本选型与性能基准

## 目的

在迁移驱动前冻结 Zenoh 版本、Key 命名、消息 schema、QoS 和容量目标，用可重复基准判断 Docker/SHM/跨机方案是否合格。

## 要求

- 先生成现状 inventory：仓库源码、base image 内部模块、配置、端口、镜像 digest 和设备能力；标明哪些内容不可复现。
- 解决配置冲突：`hz: 2` 与现有频率校验、`gige_`/`camera_` 前缀、lidar1 enabled、IMU UDP/串口和各 Web 端口。
- 补齐或替换 Compose 引用但未入库的 `configs/`、`sensors_lib.py` 等依赖；启动前禁止从 base image 偶然 import 未声明模块。
- 对四套旧 SHM magic/ring 建立 golden fixture，确认 Publisher/Subscriber 重复实现是否 wire-compatible。
- 选择并固定同一 Zenoh Rust/C++/Python/Router 版本及镜像 digest，记录 SHM API 稳定性风险。
- 定义 `rt/{site}/sensor/...` key、通用消息头、相机/点云/IMU/PLC schema v1。
- 定义实时、录制、控制、状态四类 QoS；所有队列必须有界。
- 实现 synthetic benchmark：支持 6×1080p RGB8 20 Hz、2 路可调点云、IMU/PLC。
- 分别测试 native、Docker host network、同机 SHM、A→D 10GbE。
- 用实际收到的 buffer 类型或 Zenoh 诊断证明 SHM 生效，不只比较吞吐。
- 输出 DDR/NUMA、CPU、memlock、`/dev/shm`、socket buffer 的环境记录。
- 建立 ADR：是否使用显式 SHM buffer、Python 是否允许进入相机热路径、Router 拓扑。

## 可用资源

- `设计/zenoh采集架构/README.md`
- `设计/1-Eclipse Zenoh (现代零拷贝通信框架).md`
- `resource/template_prefix_full.yaml`
- `resource/sub_client/multimodal_src/multimodal_common/shm/`
- 现有相机槽 4 MiB、点云槽 16 MiB 的配置只能作参考，不能直接视为足够。

## 交付物

- `docs/current-state-inventory.md`：缺失源码、配置冲突、镜像内部依赖和处理结论。
- `configs/`：可通过 schema 校验且 Compose 能实际加载的最小 mock 配置。
- `src/common/schema/` 与 JSON Schema/Protobuf 定义。
- `benchmarks/zenoh-data-plane/`、一键执行脚本和 JSON 结果。
- `docs/adr/` 中的版本、SHM、QoS、时间戳 ADR。
- 兼容性矩阵和容量预算。

## 测试与验收

- 所有生产 Compose 通过 `docker compose config`；引用的配置、Dockerfile 和挂载源路径都存在。
- 在不依赖开发者机器残留文件的干净环境中，mock 路径可启动。
- base image 中所需模块都有固定 digest、来源和 API 清单；真实 GigE 桩实现不会被误判为真机采集。
- schema 正反例测试，未知 major version 必须拒绝。
- 同机 10 分钟和跨机 30 分钟基准，无无界内存增长。
- 报告 p50/p95/p99 延迟、吞吐、CPU、copy/SHM 模式、drop 和 RSS。
- 目标负载下 Router/Publisher 不崩溃；达不到目标时给出瓶颈和降级策略。
- 在 6 路 RGB8 理论负载之外加入至少 20% 突发，验证 `drop/latest-only` 不阻塞采集。

## 非目标

不接厂商 SDK，不开发 Web UI，不在本任务决定具体传感器能否硬件触发。
