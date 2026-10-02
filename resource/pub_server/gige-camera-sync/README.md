# gige-camera-sync

GigE 相机采集服务。SDK 和驱动二进制已预装在 base Docker 镜像中：

```
registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:server-gige-sync-v6.8.5b-x64
```

## 开发流程

1. 从 base image 导出 Python 源码到 `gige_sync/`（若本地尚无）：

```bash
docker create --name gige-extract registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:server-gige-sync-v6.8.5b-x64
docker cp gige-extract:/app/gige_sync ./gige-camera-sync/
docker rm gige-extract
```

2. 修改 `gige_sync/` 中的 Python 代码
3. 构建 overlay 镜像（仅覆盖代码层，SDK 不变）：

```bash
docker build -f gige-camera-sync/Dockerfile -t my-gige-sync .
```

## 配置

通过 `configs/sensors.yaml` 或 `GIGE_DISCOVER_FILTER` 环境变量指定发现过滤器。
