# 基于已构建的 GigE SDK 镜像，仅覆盖配置和 Python 代码层
# SDK / MVS / 二进制保留在 base image 中，无需重新安装
#
# 首次使用请先从 base image 导出 Python 包路径:
#   docker run --rm -it --entrypoint ls <base-image> -la /app/
#
# 构建: docker build -f gige-camera-sync/Dockerfile -t <tag> .
ARG BASE_IMAGE=registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:server-gige-sync-v6.8.5b-x64
FROM ${BASE_IMAGE}

COPY configs /app/configs

# 若有本地 Python 修改，覆盖到 base image 对应路径（默认 /app/gige_sync）
COPY gige-camera-sync/gige_sync /app/gige_sync

RUN if [ -f /app/pyproject.toml ]; then pip install --no-cache-dir -e /app; fi

ENV SENSORS_CONFIG=/app/configs/sensors.yaml
ENV GIGE_DISCOVER_FILTER=/app/configs/sensors.yaml
