# 任务 04：GigE 相机 Publisher

## 目的

将 6 路 GigE 相机的唯一采集进程迁移为 Zenoh Publisher，并安全支持监控、在线参数和硬件触发。

## 要求

- 复用厂商 SDK base image，以 serial/MAC 绑定稳定 sensor id；每台相机只有一个控制 owner。
- 真机驱动必须替换当前 `driver.py` 桩实现，发布真实 device timestamp、frame counter、trigger id。
- 支持 FreeRun、PTP（设备支持时）、外部 Trigger；明确报告 sync level。
- 区分 sensor capture Hz、publish Hz、preview Hz。
- 曝光/增益在线修改后读回验证；PixelFormat/ROI/分辨率走受控重配并更新 schema/SHM pool。
- 相机断线不影响其余相机；重连后 source/sequence 语义明确。
- 在采集进程内低频读取 GenICam 健康，不再用第二进程高频打开设备。
- 接入交换机 SNMP 指标（由任务 10 提供）或清楚标记链路速率来源未知。
- 防止重复容器同时打开相机。

## 可用资源

- `resource/pub_server/gige-camera-sync/`
- `resource/pub_server/gige-camera-sync/scripts/gige_discover.py`
- `resource/template_prefix_full.yaml`
- base image：`server-gige-sync-v7.1.0-x64`（实施时固定 digest）。
- 当前 worker 的“先抓帧后按网格标时”不可直接沿用。

## 交付物

- `src/pub_server/gige-camera/`
- 6 相机配置样例、Dockerfile/Compose、真机与 mock 模式。
- 参数能力矩阵和维护扫描命令。
- 相机 payload/schema、控制命令和指标文档。

## 测试与验收

- mock 6×1080p 在 1/5/10/20 Hz 下频率和 sequence 正确。
- 真机逐台验证 serial、PixelFormat、曝光、trigger、时间戳及掉线重连。
- 同机 SHM checksum 正确，A→D 带宽/CPU 满足任务 00 阈值。
- 动态 RGB→Mono 不能产生旧 schema + 新 payload 的混合帧。
- 运行发现/健康查询时不打断采集；若设备不允许并发访问，自动使用 owner 内查询。
- 报告 packet loss、resend、frame drop、capture-to-receive latency 和 jitter。

## 非目标

不以 ping 结果冒充图像延迟，不承诺所有相机型号支持在线 PixelFormat 或 PTP。
