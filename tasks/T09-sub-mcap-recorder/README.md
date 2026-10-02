# T09 MCAP 多模态录制（迁移 client-mcap-plc-web）

## 目的

把 `resource/sub_client/client-mcap-plc-web-src` 从读取 SHM 环形缓冲改为订阅 Zenoh，录制相机/雷达/IMU/PLC 为 MCAP（Foxglove 可直接回放），保留其 Web 录制控制功能。

## 范围

做：
* 订阅 `rig/camera/*/image`、`rig/lidar/*/points`、`rig/imu/*/data`、`rig/plc/*/state`（可配置），按 encoding 转为 foxglove schema：
  `RawImage`（Bayer 原样存，encoding 字段写 `bayer_rggb8`，节省空间）或 `CompressedImage`（可选 JPEG/H.264）、`PointCloud`、IMU/PLC JSON。
* MCAP 消息 `log_time`=接收时刻，`publish_time`=`stamp_ns`；额外写入帧头字段（seq、trigger_ns）到 metadata 或单独 channel，便于离线同步。
* 录制控制：作为 `SensorNode("app", "mcap_recorder")`，ops `start{topics, split_mb, note}`、`stop`、`status`；保留旧 Web 页面或改用控制台 ctrl。
* 文件切分（大小/时长）、磁盘剩余空间保护、写盘线程与订阅线程分离（订阅回调只入队）。
* 写盘跟不上时丢帧并计数（不得阻塞总线）。

## 可用资源

* 旧代码：`web/mcap_web_viz/recorder.py`、`foxglove_multimodal_recorder.py`、`plc_multimodal_collector.py`、`main.py`、`tools/mcap_mock_shm_replay.py`。
* 镜像：`MCAP_IMAGE=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:client-mcap-web-v7.1.0-x64`。
* SDK：`sub.subscribe_frames`、`codecs.points_from_bytes`/`imu_array`；`sub-foxglove-preview/foxglove_preview.py`（schema 转换参考）。
* compose：`deploy/docker-compose.sub.yml` 中 `sub-mcap-recorder`（profile `record`，挂载 `/data/records`）。

## 交付物

`src/sub_client/sub-mcap-recorder/`：README.md、Dockerfile、recorder.py、web/（可选）、tests/

## 测试与验收

1. 与 pub-mock（`--full-res`）录制 5 分钟：Foxglove Studio 打开正常；每个 topic 消息数 = 发布数 − 统计的丢帧数；重启后能继续新文件。
2. 6 路 Bayer 1080p@20Hz + 雷达（≈ 2.1Gbit/s）持续录制 30 分钟（NVMe）：丢帧率 < 0.1%，采集端 `gaps` 不增加。
3. 磁盘剩余 < 阈值时自动停止并发事件。
