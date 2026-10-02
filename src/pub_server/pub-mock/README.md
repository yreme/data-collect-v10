# pub-mock —— 模拟全部传感器（参考 pub 实现）

按 `sensors.yaml` 中启用的设备模拟相机 / 雷达 / IMU / PLC，用于无硬件联调，也是**编写 pub 的参考代码**（`docs/03`）。

```bash
python3 pub_mock.py -c ../../configs/sensors.yaml                 # 640x360 小图
python3 pub_mock.py --full-res                                     # 1920x1080
python3 pub_mock.py --only camera,lidar --slow-link cam_side_1     # 模拟某相机百兆降速
```

| 参数 | 说明 |
| --- | --- |
| `-c/--config` | 配置文件，默认 `$SENSORHUB_CONFIG` |
| `--only` | 只模拟指定类型（camera,lidar,imu,plc） |
| `--full-res` | 相机使用配置分辨率 |
| `--max-cameras` | 最多模拟的相机数 |
| `--slow-link` | 这些相机报告 `link_speed_mbps=100`（状态 degraded） |
| `--shm-mb` | SHM 池大小 |
| `--duration` | 运行秒数（0=一直运行） |

| 类型 | key | encoding | 在线参数 |
| --- | --- | --- | --- |
| 相机 | `rig/camera/{name}/image` | bayer_rggb8 / bgr8 / mono8 | hz、publish_encoding、exposure_auto、exposure_us、gain_db；op `reconnect` |
| 雷达 | `rig/lidar/{name}/points` | xyzirt_f32 | hz（需重启）、phase_lock_deg |
| IMU | `rig/imu/{name}/data` | imu_v1（每网格周期一批） | publish_hz |
| PLC | `rig/plc/{name}/state` | json | — |

所有帧都带 `Flags.MOCK`。
