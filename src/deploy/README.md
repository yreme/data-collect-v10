# deploy —— docker compose 部署

| 文件 | 作用 |
| --- | --- |
| `common.yml` | 公共模板 `sensorhub-base` / `sensorhub-pub-base`（host 网络、ipc host、memlock 无限制、日志轮转） |
| `.env.example` | 镜像地址（复用 `设计/可复用的docker资源.md` 中的基础镜像）与运行参数 |
| `docker-compose.core.yml` | zenohd 路由器 + Web 控制台（每台服务器一份） |
| `docker-compose.mock.yml` | 无硬件演示：mock 传感器 + Foxglove 低频预览 + sub 示例 |
| `docker-compose.pub.yml` | 真实采集服务（按 profile 启用：sync / camera / lidar / imu / plc / net / all） |
| `docker-compose.sub.yml` | 消费服务（preview / record / all） |
| `examples/third-party-app/` | 其它团队的程序如何用 `extends` 接入 |
| `host-tuning.sh` | 宿主机 sysctl / MTU / 网卡 ring 调优 |

```bash
cd src/deploy
cp .env.example .env                     # 服务器 D：ZENOHD_CONFIG=zenohd.server-d.json5

# 1) 无硬件体验整套流程
docker compose -f docker-compose.core.yml -f docker-compose.mock.yml up -d --build
#   控制台  http://<server>:18100
#   Foxglove ws://<server>:8765

# 2) 真实硬件
docker compose -f docker-compose.core.yml -f docker-compose.pub.yml --profile all up -d
docker compose -f docker-compose.core.yml -f docker-compose.sub.yml --profile preview up -d
```

compose 文件可以拆开分别启动（各自 `up`/`down` 互不影响），它们之间只通过本机 zenohd（7447）通信，
不依赖 docker network。不用 docker 时，zenohd 用 `../broker/native/install.sh` 安装，
其它程序直接 `python3 xxx.py` 运行，环境变量与 compose 中一致。

## 性能要点（详见 `../docs/06-Docker部署与性能调优.md`）

1. **必须** `network_mode: host` + `ipc: host` + `ulimits.memlock: -1`，否则：
   - 没有 host 网络：多一层 veth/NAT，且 peer 间 127.0.0.1 直连失效，全部绕路由器；
   - 没有 ipc host：容器之间看不到彼此的 `/dev/shm` 段，SHM 失效；
   - memlock 不足：SDK 自动关闭 SHM（日志会 ERROR 提示），退化为 TCP 回环拷贝。
2. 本机消费者用 **peer 模式**（SDK 默认），与 pub 直连，SHM 零拷贝；`client` 模式会经路由器转发，失去 SHM。
3. 采集容器可 `cpuset` 绑核，并避开网卡中断所在核；相机采集线程可用 `SCHED_FIFO`（已加 `SYS_NICE` 与 `rtprio`）。
4. 相机发布 Bayer 原始数据（2 MB/帧）而不是 BGR（6 MB/帧），消费端按需解 Bayer。
