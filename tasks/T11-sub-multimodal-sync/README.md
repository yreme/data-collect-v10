# T11 多模态同步消费（迁移 multimodal_src）

## 目的

`resource/sub_client/multimodal_src` 从多个 SHM 环（image/pointcloud/imu）按时间戳对齐后输出到 Foxglove/MCAP/视频编码。
改为订阅 Zenoh，并用 `SyncAssembler` 基于 `trigger_ns` 对齐，删除旧的 `shm_discovery`/`timestamps` 对齐逻辑。

## 范围

* 输入：配置中的相机/雷达/IMU（`required` 必须是具体 key），`base_hz` 取公共频率（相机 20 + 雷达 10 → 10）。
* 同步组回调交给工作线程，迁移旧 `clients/`（foxglove_client、mcap_client、mcap_compress_client、video_encoder）。
* IMU 批数据按组时间窗切片附加到同步组。
* 统计：完整组率、部分组率、各路缺失次数 → status。

## 可用资源

* 旧代码：`multimodal_sync/{callback,cli,config,timestamps}.py`、`clients/*`、`Dockerfile.foxglove`。
* SDK：`sub.SyncAssembler`、`sub-template-python --mode sync`（最小可运行示例）。

## 交付物

`src/sub_client/sub-multimodal-sync/`：README.md、Dockerfile、multimodal_sync/、tests/

## 测试与验收

1. pub-mock 全传感器：完整组率 > 99.5%，组内 `trigger_ns` 全部相同。
2. 停掉一路相机：`emit_partial` 模式下输出部分组且缺失计数正确；恢复后自动回到完整组。
3. 与旧程序在同一数据下对比输出（帧对应关系）。
