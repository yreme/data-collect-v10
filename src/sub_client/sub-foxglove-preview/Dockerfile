# Foxglove 低频预览
# 默认基于可复用的 client-foxglove 镜像（已带 foxglove-sdk / numpy），只叠加 zenoh + sensorhub SDK + 本程序。
# 构建（上下文为 src/）：
#   docker build -f sub_client/sub-foxglove-preview/Dockerfile \
#     --build-arg BASE_IMAGE=$FOXGLOVE_IMAGE -t sensorhub-foxglove-preview .
ARG BASE_IMAGE=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:client-foxglove-v7.1.0-x64
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PYTHONUNBUFFERED=1

COPY common/python /opt/sensorhub/sdk
RUN pip install --no-cache-dir /opt/sensorhub/sdk \
 && (python3 -c "import foxglove" 2>/dev/null || pip install --no-cache-dir "foxglove-sdk>=0.28")

COPY sub_client/sub-foxglove-preview /app/preview
WORKDIR /app/preview
ENTRYPOINT ["python3", "/app/preview/foxglove_preview.py"]
