# pub-imu — Yesense IMU 采集

读取 Yesense IMU（UDP 或串口），按全局网格把样本打包发布到 `{prefix}/imu/{name}/data`。

## 运行

```bash
# 源码（无 docker）
python3 pub_imu.py -c ../../configs/sensors.yaml
python3 pub_imu.py --mock                    # 无硬件联调

# docker（见 deploy/docker-compose.pub.yml --profile imu）
docker compose -f docker-compose.core.yml -f docker-compose.pub.yml --profile imu up -d --build
```

## 配置（sensors.yaml → imus）

| 字段 | 说明 |
| --- | --- |
| `sample_hz` | 设备原始采样率（默认 200） |
| `publish_hz` | 网格打包发布频率（默认 20） |
| `transport` | `udp` 或 `serial`（改 transport 需重启） |
| `port` / `imu_src_port` | UDP 监听端口与源端口过滤 |
| `serial_device` / `baudrate` | 串口路径与波特率 |

## 在线参数

- `publish_hz`：下一个整秒生效，批大小 ≈ `sample_hz / publish_hz`
- `sample_hz`、`transport`：只读或 restart_required

## 测试

```bash
cd src/pub_server/pub-imu && python3 -m pytest -q
```
