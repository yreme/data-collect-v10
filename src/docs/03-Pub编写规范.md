# 03 Pub 编写规范

参考实现：`pub_server/pub-mock/pub_mock.py`（Python）。C++ pub 使用 zenoh-cpp + `common/cpp/sensorhub/frame_header.hpp`，
控制面（status/ctrl/meta/liveliness）语义必须与 Python `SensorNode` 一致（见 02）。

## 1. 最小示例

```python
from sensorhub import config as C
from sensorhub.header import image_header, Flags
from sensorhub.node import SensorNode
from sensorhub.params import ParamSpec
from sensorhub.session import open_session
from sensorhub.shm import ShmPool
from sensorhub.sync_grid import GridTimer

cfg = C.load()                                   # $SENSORHUB_CONFIG
session = open_session()                         # peer 模式，连接本机 zenohd，自动处理 SHM/memlock
pool = ShmPool(256 << 20)                        # 一个进程一个池，所有 SensorNode 共享

for dev in C.devices(cfg, "cameras"):            # 已合并 defaults + 单设备覆盖，只含 enabled
    node = SensorNode("camera", dev["name"], session=session, shm_pool=pool,
                      version="1.0.0", meta={"alias": dev.get("alias", ""), "serial": dev["serial"]})
    img = node.add_stream("image", encoding="bayer_rggb8", target_hz=dev["hz"])
    timer = GridTimer(hz=dev["hz"])
    node.add_params([ParamSpec("hz", "enum", dev["hz"], choices=[1, 5, 10, 20], unit="Hz")],
                    on_set=lambda ch: {"effective_ns": timer.set_hz(ch["hz"])} if "hz" in ch else None)
    node.set_device(ip="192.168.1.101", link_speed_mbps=1000, expected_link_mbps=1000)
    node.start()

    while True:
        slot = timer.wait_next()                 # 网格时刻（trigger_ns）
        raw, stamp_ns = camera.trigger_and_grab(slot)   # ← 真实 SDK
        img.publish(raw, image_header(encoding="bayer_rggb8", width=1920, height=1080,
                                      seq=img.next_seq(), stamp_ns=stamp_ns, trigger_ns=slot,
                                      flags=Flags.SYNC_SOFT_TRIGGER))
```

## 2. 必须遵守

| # | 规则 | 原因 |
| --- | --- | --- |
| P1 | 每个物理传感器 = 一个 `SensorNode`（kind/name 与 `sensors.yaml` 一致） | 控制台按 kind/name 关联配置、别名、状态 |
| P2 | 设备只能由一个进程打开；**不要在采集进程之外再开设备做探测** | GigE 控制通道独占，旧 `gige_discover.py` 冲突即源于此 |
| P3 | 采集循环由 `GridTimer`（或硬件触发）驱动，`trigger_ns` 填网格时刻，`stamp_ns` 填传感器时刻 | 跨容器同步依赖确定性网格（05） |
| P4 | 采集线程中只做"取数据 + publish"，不做编码/转换/写盘 | 采集线程阻塞会丢触发 |
| P5 | 大数据通道用默认 `congestion="drop"`、`shm=True` | 慢订阅者不得反压采集 |
| P6 | 发布原始格式（Bayer/原始点云），不要发布 BGR；预览/JPEG 另开 `preview` 通道且限频 | 带宽 3 倍差距；6 路 RGB8@20Hz 超过 GigE 能力 |
| P7 | 设备信息（ip、mac、serial、model、`link_speed_mbps`、`expected_link_mbps`、`rtt_ms`、温度）用 `set_device` 更新 | 控制台网络页与告警依赖这些字段 |
| P8 | 设备断开：`set_state("degraded"/"error", reason)` + 后台重连，**进程不要退出**；配置了但找不到设备 → `absent` | 保持 liveliness 与 ctrl 可用，便于远程诊断 |
| P9 | 参数用 `ParamSpec` 自描述，`live=False` 的参数在 `set_params` 返回 `restart_required` | 控制台自动生成表单 |
| P10 | 频率变更在 **下一个整秒** 生效（`GridTimer.set_hz` 默认行为），返回 `effective_ns` | 多容器同时改频率不会错拍 |
| P11 | 处理 SIGTERM：`node.close()`（发布最终 status=stopped，撤销 token） | `docker stop` 优雅退出 |
| P12 | 读配置只用 `sensorhub.config`；不要自定义配置文件 | 控制台统一编辑与版本管理 |
| P13 | 日志输出到 stdout，INFO 级别不要逐帧打印 | docker 日志轮转 |

## 3. 推荐

* 一个容器管理同类多路设备（如 6 路相机一个进程），共享 session/SHM 池，减少连接数。
* 每路设备一个采集线程；Python 中 SDK 回调线程只入队，避免 GIL 抢占造成触发抖动。若 Python 抖动超过 2ms，改 C++。
* 相机参数映射到 GenICam 名称：`hz`→`AcquisitionFrameRate`（软件触发模式下由网格控制），`exposure_us`→`ExposureTime`，
  `exposure_auto`→`ExposureAuto`，`gain_db`→`Gain`，`pixel_format`→`PixelFormat`（需停流重启，`live=False` 或内部停启）。
* 每 `link_check_interval_sec` 秒用 **已打开的句柄** 读 `GevLinkSpeed`（或 MVS `MV_CC_GetIntValue("GevLinkSpeed")`），
  低于 `expected_link_mbps` 时 `set_state("degraded", "link 100Mbps")` 并 `emit_event("warn", ...)`。

## 4. 目录与交付物

```
pub_server/pub-xxx/
  README.md        用途、配置字段、参数表、自定义 op、已知限制
  Dockerfile       FROM 可复用基础镜像（GIGE_IMAGE/LIDAR_IMAGE/IMU_IMAGE），只叠加 SDK + 本程序
  pub_xxx.py | src/*.cpp + CMakeLists.txt
  tests/           无硬件可运行的单元测试（mock 设备层）
```

`deploy/docker-compose.pub.yml` 中已预留服务名与 profile，Dockerfile 路径需与之一致。
