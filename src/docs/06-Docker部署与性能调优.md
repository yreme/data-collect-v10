# 06 Docker 部署与性能调优

## 1. 结论

**docker 本身不会降低 Zenoh/SHM 性能**，前提是使用以下三项（`deploy/common.yml` 的 `sensorhub-base` 已全部包含）：

```yaml
network_mode: host     # 无 veth/NAT/iptables；本机 peer 之间 127.0.0.1 直连
ipc: host              # 共享宿主机 /dev/shm —— 不同容器可映射同一 SHM 段
ulimits:
  memlock: -1          # Zenoh SHM 段需要 mlock
```

缺少任意一项的后果：

| 缺少 | 现象 |
| --- | --- |
| `network_mode: host` | 走 docker bridge（多一次 veth 拷贝 + NAT），peer gossip 得到的 127.0.0.1 locator 无法互通，全部流量绕路由器 |
| `ipc: host` | 容器之间 `/dev/shm` 隔离，SHM 失效，退化为 TCP 回环 |
| `memlock: -1` | 默认 8MB（docker 默认 64KB~8MB）→ SHM 分配失败 `OS error 12`；**更隐蔽的是**：订阅方 memlock 不足时会**静默丢弃**对端的 SHM 帧。SDK 的 `build_config` 已检测 memlock，不足 32MB 时自动关闭本进程 SHM 并打印 ERROR，确保至少能收到数据 |

原生（systemd）部署对应 `LimitMEMLOCK=infinity`，命令行调试用 `sudo bash -c 'ulimit -l unlimited; exec setpriv --reuid=$USER ...'`
（对已运行 shell 执行 `prlimit` 不会传递给其子进程中已启动的程序）。

## 2. 实测数据（本仓库 VM，Zenoh 1.10.1，Python SDK）

| 场景（6.2MB 帧，即 1080p BGR） | p50 延迟 |
| --- | --- |
| peer ↔ peer，SHM | **1.1 ms** |
| client 经路由器（无 SHM） | 3.7 ms |

* 只有 **peer↔peer 直连** 才是零拷贝；client 模式或经路由器转发都不是 SHM（路由器会把 SHM 帧转为字节）。
* Python 中 `ZShmMut` 只接受 bytes/bytearray，向 SHM 写入仍有一次拷贝；订阅方 `payload().to_bytes()` 6MB 约 1ms。
  对延迟极敏感的 pub（相机/雷达）建议 C++ 直接把 SDK 缓冲写入 SHM 缓冲。
* 发布 Bayer（2MB）而不是 BGR（6MB）：拷贝与跨机带宽都降为 1/3。

## 3. 部署文件

见 `deploy/README.md`。要点：

* 每个 compose 文件可独立 `up/down`；它们之间只通过本机 zenohd（7447）通信，不依赖 docker network，所以
  **不同团队可以写各自的 `docker-compose.yml`**，只要 `extends: { file: <path>/common.yml, service: sensorhub-base }`。
* 配置目录以只读挂载到 `/app/configs`（控制台为读写，保存/回滚都在宿主机 `configs/` 与 `configs/.history/`）。
* 镜像复用 `设计/可复用的docker资源.md` 中的基础镜像（`GIGE_IMAGE` 等），Dockerfile 只叠加 sensorhub SDK 与程序。

## 4. 宿主机调优（`deploy/host-tuning.sh`）

| 项 | 建议 | 说明 |
| --- | --- | --- |
| MTU | 相机网口 9000（jumbo），交换机同步开启 | GigE Vision 包大小 `GevSCPSPacketSize=8164`，CPU 中断数降为 1/6 |
| socket 缓冲 | `net.core.rmem_max=128MB`，`rmem_default=32MB` | GigE 流 UDP 突发 |
| 网卡 ring | `ethtool -G <if> rx <max>` | 6 路同时触发形成微突发 |
| 中断亲和 | 网卡 IRQ 绑到独立核（`irqbalance` 关闭或 ban 掉这些核） | |
| CPU 绑核 | 采集容器 `cpuset` 避开 IRQ 核；AI 容器另用其它核 | 减少触发抖动 |
| 实时调度 | 采集线程 `SCHED_FIFO`（容器已加 `SYS_NICE` + `rtprio`） | 软件触发抖动 |
| 时钟 | `linuxptp`（相机/雷达 PTP）+ A/D 之间 PTP 或 chrony | 同步前提 |
| swap | `vm.swappiness=1` | |

## 5. 带宽预算（6 路相机 1920×1080 @20Hz）

| 格式 | 单路 | 6 路 | 结论 |
| --- | --- | --- | --- |
| Bayer8 | 41 MB/s = 332 Mbit/s | 2.0 Gbit/s | 单相机 GigE 余量充足；A→D 10GbE 充足 |
| Mono8 | 同上 | 同上 | |
| RGB8/BGR8 | 124 MB/s = 995 Mbit/s | 6.0 Gbit/s | **超过单相机 GigE 链路**（相机侧需降帧到 ~15Hz 以下），不推荐 |

在控制台切换 RGB/Mono 时注意：相机侧 `PixelFormat` 决定线缆带宽；`publish_encoding` 决定总线带宽。
