# sensorhub —— 基于 Eclipse Zenoh 的传感器数据总线

替代 `resource/` 中的自研 SHM 环形缓冲：一个进程独占采集一个（或一类）传感器，通过 Zenoh 发布；
本机程序 SHM 零拷贝订阅，另一台服务器经 zenohd 路由自动获取；Web 控制台统一查看、控制、管理配置。

```
src/
  common/python/sensorhub   Python SDK（pip install ./common/python）
  common/cpp/sensorhub      C++ 帧头（与 Python 字节级一致）
  configs/sensors.yaml      唯一的传感器配置（控制台编辑 + 版本历史）
  broker/                   zenohd：docker-compose 与原生 systemd 两种部署
  console/                  Web 控制台（:18100）
  pub_server/pub-mock       参考 pub：模拟相机/雷达/IMU/PLC
  sub_client/sub-template-python   参考 sub：latest / sync / stats 三种模式
  sub_client/sub-foxglove-preview  低频（1Hz~0.1Hz）转 Foxglove WebSocket 预览
  deploy/                   compose 模板、分文件部署、宿主机调优
  docs/                     设计与规范（先读 01、02、03/04）
  tests/e2e_mock_stack.py   端到端测试
../tasks/                   拆分给其他人实现的任务（每个目录一个任务）
```

## 快速开始（无硬件，无 docker）

```bash
pip install ./src/common/python -r ./src/console/requirements.txt foxglove-sdk
# zenohd：broker/native/install.sh 安装，或下载 zenoh standalone 包直接运行
zenohd -c src/broker/config/zenohd.json5 &

python3 src/pub_server/pub-mock/pub_mock.py -c src/configs/sensors.yaml &
python3 -m sensorhub_console --config-dir src/configs --data-dir /tmp/console   # 在 src/console 目录下
python3 src/sub_client/sub-foxglove-preview/foxglove_preview.py --hz 1 &
# 浏览器 http://127.0.0.1:18100 ；Foxglove 连接 ws://127.0.0.1:8765
```

`SHM` 需要 `ulimit -l unlimited`（否则 SDK 自动退化为 TCP 并打印 ERROR），见 `docs/06`。

## docker

```bash
cd src/deploy && cp .env.example .env
docker compose -f docker-compose.core.yml -f docker-compose.mock.yml up -d --build
```

## 测试

```bash
cd src/common/python && python3 -m pytest -q          # SDK（含 zenoh 集成，需本机可起 session）
cd src/console && python3 -m pytest -q                # 控制台
python3 src/tests/e2e_mock_stack.py                   # 端到端：zenohd + mock + console + preview + sub
g++ -std=c++17 -I src/common/cpp src/common/cpp/tests/test_frame_header.cpp -o /tmp/t && /tmp/t
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [01-架构设计](docs/01-架构设计.md) | 拓扑、组件、数据面/控制面 |
| [02-Key命名与消息格式规范](docs/02-Key命名与消息格式规范.md) | key、64B 帧头、编码、ctrl 协议、status 字段 |
| [03-Pub编写规范](docs/03-Pub编写规范.md) | pub 示例与必须遵守的规则 |
| [04-Sub编写规范](docs/04-Sub编写规范.md) | sub 示例、同步组、跨机、解码 |
| [05-时间同步与触发设计](docs/05-时间同步与触发设计.md) | 全局网格、相机/雷达/IMU 同步方式 |
| [06-Docker部署与性能调优](docs/06-Docker部署与性能调优.md) | host/ipc/memlock、实测数据、宿主机调优、带宽预算 |
| [07-网络链路监控方案](docs/07-网络链路监控方案.md) | 替代 gige_discover、链路速率/延迟、相机动态控制 |
