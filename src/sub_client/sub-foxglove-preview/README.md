# sub-foxglove-preview —— 低频 Foxglove 预览

把总线数据以低频（默认 1Hz，可 0.1Hz 即 10 秒一帧）转换为 Foxglove WebSocket，仅用于查看。

```bash
python3 foxglove_preview.py --hz 1 --port 8765
python3 foxglove_preview.py --hz 0.1 --keys rig/camera/cam_corner_0/image,rig/lidar/lidar0/points
# Foxglove Studio -> Open connection -> Foxglove WebSocket -> ws://<server>:8765
```

| 数据 | 转换 |
| --- | --- |
| 相机 | Bayer → RGB（2×2 合并）、缩放到 `--max-width`，`RawImage` |
| 雷达 | 抽稀到 `--max-points`，`PointCloud` |
| IMU / PLC | JSON |

抽帧按网格对齐（`max_hz`），多路相机抽到的是同一时刻。程序自身注册为 `rig/app/foxglove_preview`，
`preview_hz`、`max_width`、`max_points` 可在控制台在线修改。增强计划见 `tasks/T12`。
