# T12 Foxglove 预览增强

## 目的

`src/sub_client/sub-foxglove-preview` 已能以 1Hz~0.1Hz 把相机/点云/IMU/PLC 推送到 Foxglove WebSocket。本任务提升可用性与带宽效率。

## 范围

1. 图像改为 `CompressedImage`（JPEG，质量可配），远程查看带宽降低 10 倍以上；保留 RawImage 选项。
2. 发布 `FrameTransform`（各传感器 frame_id → 车体，外参来自 `sensors.yaml` 新增 `extrinsics` 字段）与 `CameraCalibration`（内参）。
3. PLC 数据生成 Foxglove 场景（迁移 `plc-data-collect/foxglove`、`foxglove_messages.py` 中的起重机可视化）。
4. 每路可单独开关/设置频率（参数 `topics`、`per_topic_hz`），控制台在线修改。
5. 同步预览模式：只推送 `trigger_ns` 相同的一组（相机 + 点云同一时刻），方便检查同步。
6. 订阅者为 0 时不解码（检测 Foxglove 客户端连接数）。

## 可用资源

* 现有：`foxglove_preview.py`（Bayer→RGB、抽稀、参数）；foxglove-sdk 0.28（`foxglove.messages`、`foxglove.channels`）。
* 镜像：`FOXGLOVE_IMAGE=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:client-foxglove-v7.1.0-x64`。
* 旧代码：`resource/pub_server/plc-data-collect/foxglove/`、`resource/sub_client/multimodal_src/multimodal_sync/clients/foxglove_client.py`。

## 交付物

`sub-foxglove-preview` 代码更新、README.md、tests/（转换函数单元测试：Bayer 四种排列颜色正确、JPEG 尺寸、TF 生成）

## 测试与验收

1. Foxglove Studio 连接后 3D 面板中点云与相机视锥位置正确（外参生效）。
2. 0.1Hz 模式下 6 路相机 + 点云的上行带宽 < 200 KB/s。
3. 无客户端连接时 CPU 占用 < 2%。
