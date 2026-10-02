# sub-template-python —— 消费端模板（参考 sub 实现）

AI / 分析程序从总线取数据的标准写法（`docs/04`），复制本目录即可开始。

```bash
python3 sub_example.py --mode latest --keys 'rig/camera/*/image'      # 每路只处理最新帧，结果发布到 rig/app/sub_example/objects
python3 sub_example.py --mode sync --keys rig/camera/cam_corner_0/image,rig/lidar/lidar0/points --base-hz 10
python3 sub_example.py --mode stats --keys 'rig/**'                    # 只读帧头：频率、传输延迟
```

* `--mode sync` 的 key 必须是具体 key（不能含 `*`），用于判断同步组是否到齐。
* `--name` 为本程序在总线上的实例名（`rig/app/{name}`），控制台可见。
* 推理镜像：Dockerfile 的 `BASE_IMAGE` 换成自己的 CUDA/TensorRT 镜像，只叠加 sensorhub SDK。
