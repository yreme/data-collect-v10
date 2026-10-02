# 任务 09：MCAP 录制与 AI Subscriber 模板

## 目的

提供两类明确不同的消费者：尽量完整录制的 MCAP Recorder，以及追求低延迟、只处理最新帧的 AI 模板。

## 要求

- Recorder 按 sensor/key 选择订阅，保存 schema、metadata、原始 payload 和配置 revision。
- 文件轮转、磁盘配额、低空间保护、原子关闭与索引恢复。
- 录制队列有界；过载时记录精确 sequence gap，不得静默丢帧。
- AI 模板使用 latest-only，回调仅转交 buffer；提供 Python/C++ 示例。
- 支持单传感器、prefix、多相机同步组订阅。
- 多传感器组按 capture timestamp/trigger id 匹配，超时输出缺席列表。
- MCAP 回放可以重新发布到隔离 namespace，不能误发到生产 control key。

## 可用资源

- `resource/sub_client/client-imu-record-src/`
- `resource/sub_client/client-mcap-plc-web-src/`
- `resource/sub_client/multimodal_src/multimodal_sync/clients/mcap_client.py`
- base image：`client-mcap-web-v7.1.0-x64`。

## 交付物

- `src/sub_client/recorder/`
- `src/sub_client/examples/ai-python/`
- `src/sub_client/examples/ai-cpp/`
- 回放工具、磁盘容量计算器和恢复说明。

## 测试与验收

- 录制→回放后 payload checksum、sequence、capture timestamp 和 schema 一致。
- 磁盘满、进程 SIGTERM、文件轮转边界、Router 断开测试后 MCAP 可读取。
- AI 处理速度为输入 1/10 时 RSS 和 queue depth 有界，延迟保持最新帧级别。
- 同步组测试覆盖缺帧、迟到、时钟跳变和 source restart。
- 报告目标负载下磁盘吞吐、CPU、drop 和文件增长率。

## 非目标

不使用“reliable + 无限 block”拖住采集来保证录制；需要绝对无损时应另行设计传感器侧缓存/冗余链路。
