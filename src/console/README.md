# sensorhub console —— Web 控制台

```bash
pip install ../common/python -r requirements.txt
python3 -m sensorhub_console --config-dir ../configs --data-dir /tmp/console   # http://127.0.0.1:18100
python3 -m sensorhub_console --no-zenoh ...   # 只做配置/别名管理（无总线）
```

环境变量：`CONSOLE_HOST` `CONSOLE_PORT`(18100) `SENSORHUB_CONFIG_DIR` `SENSORHUB_CONFIG_FILE`(sensors.yaml)
`SENSORHUB_DATA_DIR`(别名库) 以及 SDK 的 `ZENOH_*` / `SENSORHUB_PREFIX`。

## 页面

| 页 | 内容 |
| --- | --- |
| 总览 | 配置中的全部传感器 + 总线上发现的实例：状态（running/stale/offline/disabled/degraded）、每个通道的实际频率/目标频率、带宽、延迟、同步误差、丢帧、SHM、链路速率；别名/备注/位置在线编辑（存 `aliases.json`，重启保留，优先于 yaml 中的 alias） |
| 传感器控制 | 根据实例 `describe` 自动生成参数表单（频率、RGB/Mono、曝光……），调用 `set_params` 与自定义 op |
| 配置管理 | 编辑 `sensors.yaml` → 校验 → 保存（自动生成版本）；历史列表、diff、回滚、tag（`known_good` 等，tag 版本不被清理） |
| 网络链路 | 相机自报链路速率/RTT + netprobe 结果 |
| 事件 | 各实例 event 流 |
| 系统 | zenohd 路由器信息、控制台信息 |

配置历史存放在 `{config-dir}/.history/<文件名>/`（`index.json` + `vNNNNNN_<时间>.yaml`），启动时若发现文件被外部修改会自动补一个快照。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/overview` | 合并后的传感器/实例/通道列表 |
| GET | `/api/events` | 最近事件 |
| GET | `/api/net` | 链路信息 |
| GET | `/api/router` | `@/*/router` |
| GET | `/api/probe?key=…&seconds=…` | 临时订阅某 key，只读帧头，返回频率/延迟/是否 SHM |
| GET/POST | `/api/aliases`、`/api/aliases/{kind}/{name}` | 别名/备注/位置 |
| POST | `/api/ctrl/{kind}/{name}` | 透传 ctrl：`{"op": "set_params", "params": {...}}` |
| GET/POST | `/api/config` | 读取 / 保存（`{"content": ..., "comment": ..., "author": ..., "force": false}`，校验失败返回 422） |
| POST | `/api/config/validate` | 只校验（`{"content": ...}`） |
| GET | `/api/config/history`、`/api/config/history/{v}` | 历史列表 / 某版本内容 |
| GET | `/api/config/diff?a=&b=` | unified diff（省略 b 表示与当前文件比较） |
| POST | `/api/config/rollback/{v}`、`/api/config/tag/{v}` | 回滚 / 打标签 |
| GET | `/api/info` | 版本、路径 |

保存或回滚后，控制台在 `{prefix}/cfg/changed` 发布通知，并在 `{prefix}/cfg/sensors` 提供当前配置 queryable。
