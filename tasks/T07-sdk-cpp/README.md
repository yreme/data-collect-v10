# T07 C++ SDK（zenoh-cpp）

## 目的

为 C++ 程序（雷达 T02、对延迟敏感的相机实现、C++ AI 推理程序）提供与 Python `sensorhub` **行为一致**的 SDK，
避免每个 C++ 程序各自实现 status/ctrl/meta/liveliness。

## 范围

做（header-only 或静态库，C++17）：
* `Session open_session()`：与 Python `build_config` 相同的环境变量（`ZENOH_MODE/CONNECT/LISTEN/SHM/CONFIG`）与 memlock 检测（不足自动关闭 SHM 并报错）。
* `SensorNode`：liveliness token、`status`（1Hz JSON，字段与 Python 一致）、`meta`/`ctrl` queryable、`event`、`set_device`、`set_state`。
* `ParamSpec`/`ParamSet`：同 Python 的 JSON describe 格式；`set_params` 校验 + 回调。
* `Stream::publish(span<const uint8_t>, FrameHeader)` 与 **零拷贝写法** `Stream::loan(size)` → 直接填充 SHM 缓冲 → `publish(buf, hdr)`。
* `StreamStats`（rate/bytes/latency/sync_err p50/p95/max、gaps）。
* `GridTimer`（`clock_nanosleep` + 末段忙等）、`subscribe_frames`、`SyncAssembler`。
* CMake 包 `sensorhub::sensorhub`，依赖 zenohc/zenohcxx 1.10.x、nlohmann_json。

## 可用资源

* `src/common/cpp/sensorhub/frame_header.hpp`（已完成，字节级与 Python 一致）及 `tests/test_frame_header.cpp`。
* Python 实现即规范：`src/common/python/sensorhub/{node,params,stats,sync_grid,sub,session}.py`。
* zenoh-cpp 文档与示例（`z_pub_shm`）。

## 交付物

`src/common/cpp/`：`CMakeLists.txt`、`sensorhub/*.hpp`、`src/*.cpp`、`examples/pub_example.cpp`、`examples/sub_example.cpp`、`tests/`。

## 测试与验收

1. 跨语言：C++ pub → Python sub（帧头、payload、SHM 标志），Python pub → C++ sub；C++ 实例在控制台中与 Python 实例显示一致（状态、参数表单可用）。
2. ctrl 协议兼容：用 Python `ctrl_call` 调用 C++ 节点的 describe/get_params/set_params/自定义 op。
3. 性能：6MB 帧 peer SHM，`loan` 写法发布到订阅回调 p50 < 0.3ms（记录到 `docs/06`）。
4. ASan/UBSan 下单元测试通过。
