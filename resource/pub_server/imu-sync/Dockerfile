# 基于已构建的 IMU 镜像，仅覆盖 Python 代码层
# 构建: docker build -f imu-sync/Dockerfile -t <tag> .
ARG BASE_IMAGE=registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:server-imu-sync-v6.6.24-x64
FROM ${BASE_IMAGE}

COPY imu-sync/imu_sync /app/imu_sync
COPY imu-sync/configs /app/configs
COPY imu-sync/pyproject.toml /app/pyproject.toml

RUN pip install --no-cache-dir -e /app

ENV SENSORS_CONFIG=/app/configs/sensors.yaml
# entrypoint: imu-sync (来自 base image)
CMD ["server", "-c", "/app/configs/sensors.yaml"]
