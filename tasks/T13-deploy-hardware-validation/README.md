# T13 真机 docker 部署验证与性能对比

## 目的

在服务器 A/D + 交换机 C + 全部传感器上验证 `src/deploy` 的 docker 部署，量化"docker + Zenoh"相对旧 SHM 方案的性能，
确认 docker 没有带来性能损失，并把调优参数固化。框架开发环境中**没有 docker 与硬件**，compose 仅做过静态校验（`docker compose config`）。

## 范围

1. 构建全部镜像（`docker compose -f docker-compose.core.yml -f docker-compose.mock.yml build`，以及 pub/sub profile），修复构建问题（基础镜像内 Python 版本、pip 源）。
2. A：core + pub(all) + sub(preview, record)；D：core（`ZENOHD_CONFIG=zenohd.server-d.json5`）+ 一个 AI 示例 sub。
3. 运行 `host-tuning.sh`（MTU 9000、socket 缓冲、ring），绑核方案写入 compose（`cpuset`）。
4. 基准测试（每项 10 分钟，记录 p50/p95/max）：

| 指标 | 旧 SHM | 新：原生进程 | 新：docker |
| --- | --- | --- | --- |
| 相机帧 采集→同机订阅 延迟 | | | |
| 雷达帧 采集→同机订阅 延迟 | | | |
| A→D 订阅延迟（6 路相机） | — | | |
| 采集进程 CPU / 内存 | | | |
| 订阅者数量 1→5 时采集端 CPU 变化 | | | |
| 同步误差（软件触发 / Action） | | | |

5. 故障演练：重启 zenohd（pub/sub 自动重连，时间）、kill pub 容器（控制台 offline 时间）、D 断网恢复。
6. 验证 memlock 未放开时的日志提示与退化行为（应能收到数据，但非 SHM）。

## 可用资源

* `src/deploy/*`、`src/broker/*`、`src/docs/06`；测试工具 `sub-template-python --mode stats`、控制台 `/api/probe`。
* 旧系统：`resource/可复用的docker资源/*/docker-compose.yml`。

## 交付物

* 修复后的 compose/Dockerfile；`src/docs/06` 中补充实测表与最终调优参数；`src/deploy/README.md` 中补充 A/D 部署步骤。

## 测试与验收

* 上表全部填写；docker 与原生的延迟差 < 5%；A→D 6 路 Bayer 20Hz 无持续丢帧。
* 故障演练全部自动恢复，恢复时间记录在文档中。
