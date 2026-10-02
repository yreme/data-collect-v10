# 727R 起重机 PLC UDP 数据采集

针对罗东堆场起重机 **727R** 的 PLC UDP 数据，提供实时监控、**双档 MCAP 自动归档**、Web 状态面板与 Docker 一键部署。

## 协议说明

- **传输**: UDP · 端口 `12730`
- **帧格式**: `AA BB [长度] [UDP_ClientSend] CC DD`
- **载荷**: SMH/OICR 位信号 + MH/MT/MC/CNTRH 位置 + 心跳（2 Hz）

## MCAP 双档归档

| 档位 | 轮转周期 | 文件名 | 保留策略 | 挂载目录 |
|------|----------|--------|----------|----------|
| 1 小时档 | 3600 秒 | `起-止.mcap`（北京时间） | 默认 30 天 | `./data/mcap/hourly` |
| 10 分钟档 | 600 秒 | `起-止.mcap`（北京时间） | 1 天 | `./data/mcap/short` |

示例文件名：`20250916_141258-20250916_151258.mcap`

## 快速启动

```bash
# 复制环境变量模板（可选）
cp .env.example .env

# 生产模式（真实 UDP）
./run-docker.sh up
# 或: docker compose up -d --build

# 免构建开发模式（直接使用基础镜像 + 挂载代码，适合 build 失败时）
./run-docker.sh dev

```

### 宿主机原生 Python 启动

```bash
./run-native.sh
```

| 服务 | 地址 |
|------|------|
| Web 监控 | http://localhost:8080 |
| UDP 监听 | `0.0.0.0:12730`（host 网络模式，直接绑定宿主机端口） |
| 1 小时 MCAP | `./data/mcap/hourly/` |
| 10 分钟 MCAP | `./data/mcap/short/` |

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MCAP_ENABLED` | `true` | 启用 MCAP 录制 |
| `MCAP_DEDUP_ENABLED` | `true` | 仅 PLC 有效字段变化时写入 MCAP（忽略心跳/时间戳） |
| `MCAP_HOURLY_ROTATION_SECONDS` | `3600` | 1 小时档轮转 |
| `MCAP_SHORT_ROTATION_SECONDS` | `600` | 10 分钟档轮转 |
| `MCAP_HOURLY_RETENTION_DAYS` | `30` | 1 小时档保留天数（`0`/`none` 为长期） |
| `MCAP_SHORT_RETENTION_DAYS` | `1` | 10 分钟档保留天数 |
| `HEALTH_TIMEOUT` | `5` | UDP 通路超时（秒） |
| `SCENE_FRAME_ID` | `ground` | Foxglove 3D 场景坐标系 |
| `SCENE_TROLLEY_FIXED_HEIGHT_M` | `30` | 小车轨道固定高度（米） |
| `SCENE_GROUND_SIZE_M` | `80` | 地面参考平面边长（米） |
| `SCENE_MARKER_SIZE_M` | `2.0` | MC/MT 标记立方体尺寸（米） |
| `SCENE_GROUND_ALPHA` | `0.08` | 地面平面透明度 |
| `MCAP_EMPTY_MAX_UNIQUE_TIMESTAMPS` | `2` | 时间戳种类 ≤ 此值时文件名追加 `-empty` |

## Web 功能

- UDP 通路状态（等待/正常/中断）
- UDP 数据质量（解析错误、帧头/帧尾异常、包速率、心跳）
- 变量变化实时列表（位置、信号、心跳）
- 双档 MCAP 录制状态与最近 10 个文件路径
- 5 分钟滚动位置/速度/心跳图表

## 本地开发

```bash
cd python
pip install -r requirements.txt
python3 monitor.py --mock          # Mock 模式
python3 monitor.py --no-mock       # 真实 UDP
python3 test_udp_parser.py
python3 test_mcap_recorder.py
```

## Docker 基础镜像

基于 `registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:client-mcap-web-viz-v6.8.6b-x64`，内置 MCAP Python SDK。

### 构建失败排查

若 `docker compose build` 报 iptables / 网络相关错误，可尝试：

1. **免构建模式**：`docker compose --profile dev up -d crane-monitor-dev`
2. **构建时使用 host 网络**（已在 `docker-compose.yml` 中配置 `build.network: host`）
3. 宿主机缺少 `iptables` 时，服务已默认使用 `network_mode: host`（无需端口映射，UDP 直接监听宿主机网卡）
4. 宿主机安装 `iptables` 后也可改回 bridge 模式，或设置 `DOCKER_BUILDKIT=0 docker compose build`

数据目录通过 volume 挂载到宿主机 `./data/mcap/hourly` 与 `./data/mcap/short`。

## Foxglove 回放

1. 从 Web 下载 MCAP，或从 `./data/mcap/hourly/` / `./data/mcap/short/` 取文件
2. 打开 [Foxglove Studio](https://foxglove.dev/download)
3. 导入布局 `/foxglove/layout.json`
