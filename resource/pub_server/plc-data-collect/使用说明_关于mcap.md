# MCAP 双档归档说明

## 1. 双档 MCAP 自动保存

数据通路正常时，**同时**写入两档 MCAP 文件（Foxglove 兼容）：

| 档位 | 周期 | 保留 | 挂载目录 |
|------|------|------|----------|
| 1 小时档 | 3600 秒 | 30 天（可配置为长期） | `./data/mcap/hourly` |
| 10 分钟档 | 600 秒 | 1 天 | `./data/mcap/short` |

文件名格式（北京时间）：`20250916_141258-20250916_151258.mcap`

Topics：
- `/crane727r/plc/raw`
- `/crane727r/kinematics/position`
- `/crane727r/kinematics/velocity`
- `/crane727r/signals/smh`
- `/crane727r/signals/oicr`
- `/crane727r/heartbeat`
- `/crane727r/connection`
- `/crane727r/spreader/scene`

## 2. UDP 通路健康

真实 UDP 模式下：

- `waiting`：启动后尚未收到数据 → **不采集、不录 MCAP**
- `connected`：正常收到数据 → 图表 + 双档 MCAP 录制
- `disconnected`：超时未收到 → 红色告警，**停止图表与 MCAP**

## 3. Docker 部署

```bash
# 生产（真实 UDP）
docker compose up -d --build

# Mock 演示
docker compose --profile mock up -d --build
```

| 项目 | 值 |
|------|-----|
| Web 监控 | http://localhost:8080 |
| UDP 监听 | `:12730` |
| 1 小时 MCAP | `./data/mcap/hourly/` |
| 10 分钟 MCAP | `./data/mcap/short/` |

环境变量：

- `MCAP_HOURLY_ROTATION_SECONDS=3600`
- `MCAP_SHORT_ROTATION_SECONDS=600`
- `MCAP_HOURLY_RETENTION_DAYS=30`（设为 `0` 或 `none` 表示长期保存）
- `MCAP_SHORT_RETENTION_DAYS=1`
- `HEALTH_TIMEOUT=5`
- `MOCK_MODE=false`

基础镜像：`registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:client-mcap-web-viz-v6.8.6b-x64`

## 4. Web 监控面板

- UDP 通路状态与数据质量（解析错误、帧头/帧尾异常、包速率）
- 变量变化实时列表（位置、信号、心跳）
- 双档 MCAP 录制状态
- 最近 10 个文件路径与下载链接

## 5. Foxglove 回放

1. 从 Web 下载 MCAP，或从 `./data/mcap/hourly/` / `./data/mcap/short/` 取文件
2. 打开 Foxglove Studio → **Open local file**
3. 导入布局 `/foxglove/layout.json`
