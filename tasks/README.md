# 任务拆分（基于 sensorhub 框架）

框架（`src/`）已完成：SDK、broker、Web 控制台、mock pub、sub 模板、Foxglove 预览、compose 模板、文档与端到端测试。
以下任务可以**并行**分给不同的人，每个目录一个任务，README 中包含：目的、范围、可用资源、接口约定、交付物、测试与验收。

**开始任何任务前必读**：`src/docs/01`、`02`，写 pub 读 `03`，写 sub 读 `04`；跑通 `src/tests/e2e_mock_stack.py`。

| 编号 | 任务 | 类型 | 依赖 | 需要硬件 |
| --- | --- | --- | --- | --- |
| [T01](T01-pub-camera-gige/README.md) | GigE 相机采集 pub（MVS，触发同步，动态参数，链路速率） | pub / Python 或 C++ | — | 相机 ×1~6 |
| [T02](T02-pub-lidar-rs/README.md) | 速腾雷达采集 pub（rs_driver，相位锁定） | pub / C++ | T07 | 雷达 ×1~2 |
| [T03](T03-pub-imu/README.md) | IMU 采集 pub（Yesense，按网格打包） | pub / Python | — | IMU |
| [T04](T04-pub-plc/README.md) | PLC 数据 pub（727R UDP） | pub / Python | — | 可用 mock_sender |
| [T05](T05-sync-master/README.md) | 同步主节点：网格计划、PTP 状态、同步质量监控 | 服务 | — | 可选 PTP 网卡 |
| [T06](T06-netprobe/README.md) | 网络链路探测（GVCP 被动发现 / ICMP / SNMP） | 服务 | — | 交换机 C（SNMP） |
| [T07](T07-sdk-cpp/README.md) | C++ SDK：zenoh-cpp 上的 SensorNode/Stream/订阅 | SDK | — | 否 |
| [T08](T08-console-enhancements/README.md) | 控制台增强（链路图表、多服务器、权限、批量控制） | Web | T06（部分） | 否 |
| [T09](T09-sub-mcap-recorder/README.md) | MCAP 多模态录制（迁移 client-mcap-plc-web） | sub | — | 否 |
| [T10](T10-sub-imu-record/README.md) | IMU 记录（迁移 client-imu-record） | sub | — | 否 |
| [T11](T11-sub-multimodal-sync/README.md) | 多模态同步消费（迁移 multimodal_src） | sub | — | 否 |
| [T12](T12-foxglove-preview-enhancements/README.md) | Foxglove 预览增强（压缩图、TF、标定） | sub | — | 否 |
| [T13](T13-deploy-hardware-validation/README.md) | 真机 docker 部署验证与性能对比（新 vs 旧 SHM） | 测试 | T01~T03 | 全套 |
| [T14](T14-ci/README.md) | CI：单元测试、e2e、镜像构建 | 工程 | — | 否 |

```
T07 ──► T02 ──┐
T01 ──────────┼──► T13
T03 ──────────┘
T06 ──► T08（网络页）
其余互不依赖
```

## 通用约定（所有任务适用）

1. 代码放在 `src/pub_server/<name>` 或 `src/sub_client/<name>`，每个目录含 `README.md`、`Dockerfile`、`tests/`。
2. 只使用 `sensorhub` SDK 的 key/帧头/控制面；**不得**自定义 key 格式或新的配置文件（新增字段写进 `src/configs/sensors.yaml`
   并在 `sensorhub/config.py` 的 `validate` 中补校验）。
3. 镜像：Dockerfile 以 `设计/可复用的docker资源.md` 中的基础镜像为 `BASE_IMAGE`，构建上下文为 `src/`；
   compose 服务名与 `src/deploy/docker-compose.pub.yml` / `docker-compose.sub.yml` 中预留的一致。
4. 容器必须 extends `sensorhub-base`（host 网络 / ipc host / memlock 无限制），见 `src/docs/06`。
5. 无硬件也能跑的单元测试（mock 设备层）；有硬件的验收按各任务"测试与验收"执行，并把结果（截图/日志/数字）附在 PR。
6. 控制台无需改动即可看到新实例（自描述参数），如需前端改动，归 T08。
7. 服务器 A/D 共用一份配置时，pub 用 `C.devices(cfg, section, host=C.host_id(cfg))` 只打开本机设备（设备可选字段 `host`）。

## 任务 README 模板

```
# Txx 标题
## 目的            为什么做，解决什么问题
## 范围            做什么 / 不做什么
## 可用资源        旧代码、SDK、镜像、硬件、文档
## 接口约定        key、encoding、参数（ParamSpec）、自定义 op、status.device 字段、配置字段
## 交付物          目录、文件
## 测试与验收      单元测试 + 硬件验收 + 量化指标
```
