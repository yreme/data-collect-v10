# pub-plc — 727R 起重机 PLC UDP

监听 UDP 端口，解析 727R 协议，发布扁平 JSON 到 `{prefix}/plc/{name}/state`。

## 运行

```bash
python3 pub_plc.py -c ../../configs/sensors.yaml
python3 pub_plc.py --mock

# 配合 mock_sender 联调（无实机）
python3 ../../resource/pub_server/plc-data-collect/python/mock_sender.py --port 12730 &
python3 pub_plc.py
```

## 配置（sensors.yaml → plcs）

| 字段 | 说明 |
| --- | --- |
| `udp_port` | 监听端口（默认 12730） |
| `health_timeout_sec` | 无包超时 → `degraded`（默认 5s） |

## 测试

```bash
cd src/pub_server/pub-plc && python3 -m pytest -q
```
