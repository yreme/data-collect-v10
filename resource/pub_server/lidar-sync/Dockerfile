# 基于已构建的 lidar SDK 镜像，仅覆盖 Python 代码层
# 构建: docker build -f lidar-sync/Dockerfile -t <tag> .
ARG BASE_IMAGE=registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:server-lidar-sync-v6.6.24-x64
FROM ${BASE_IMAGE}

# rs_driver / lidar-capture 二进制保留在 base image 中
COPY lidar-sync/lidar_sync /app/lidar_sync
COPY configs /app/configs

RUN pip install --no-cache-dir -e /app

ENV SENSORS_CONFIG=/app/configs/sensors.yaml
# entrypoint: lidar-sync (来自 base image)
CMD ["server", "-c", "/app/configs/sensors.yaml"]
