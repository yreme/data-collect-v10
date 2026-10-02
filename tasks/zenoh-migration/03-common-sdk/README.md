# 任务 03：Common SDK 与 Pub/Sub 示例

## 目的

为所有 Publisher/Subscriber 提供一致的 Python/C++ 封装，避免每个团队自行实现命名、SHM、metadata、重连和指标。

## 要求

- 提供 C++ 与 Python SDK；热路径优先 C++，两者 wire format 完全兼容。
- 封装 session、显式 SHM 分配、普通网络回退、有界队列、latest-only、schema 校验。
- 提供 `SensorPublisher`、`SensorSubscriber`、catalog heartbeat、metrics、control/query helper。
- 自动填充 source instance、sequence、publish time 和 config revision；capture time 只能由驱动提供。
- 断线自动恢复；回调线程禁止执行阻塞任务。
- 提供 mock Publisher、打印/统计 Subscriber、SHM→Zenoh 临时迁移 bridge。
- 明确 buffer 生命周期，Subscriber 不得在引用失效后继续访问零拷贝内存。
- API major version 变更有迁移说明。

## 可用资源

- 任务 00 的 schema/QoS/版本 ADR。
- 现有 ring 实现：`resource/sub_client/multimodal_src/multimodal_common/shm/`
- 现有客户端抽象：`resource/sub_client/multimodal_src/multimodal_sync/clients/base.py`
- 现有配置加载：`resource/sub_client/multimodal_src/multimodal_common/unified_config.py`

## 交付物

- `src/common/cpp/`、`src/common/python/`
- `src/sub_client/examples/`
- `src/common/bridges/legacy_shm_to_zenoh/`
- API 文档、编码规范、错误码与最小 Dockerfile。

## 测试与验收

- Python↔C++ 四种 pub/sub 组合互通。
- 同机 SHM 和跨机 fallback 都通过 payload checksum。
- Publisher 崩溃、Subscriber 慢、Router 重启、SHM 耗尽测试有确定行为。
- latest-only 在消费者慢 10 倍时 queue depth 不增长且报告 drop。
- 运行 AddressSanitizer/ThreadSanitizer 或等价检查，验证 buffer 生命周期。
- 示例能用一条命令启动，并显示 Hz、带宽、延迟和 sequence gap。

## 非目标

不封装厂商 SDK，不定义业务 UI，不为每个 AI 框架提供适配。
