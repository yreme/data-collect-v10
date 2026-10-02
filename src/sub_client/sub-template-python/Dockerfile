# sub 模板镜像：AI 程序可直接以自己的推理镜像为 BASE_IMAGE（CUDA/TensorRT 等），只叠加 SDK。
# 构建（上下文为 src/）：docker build -f sub_client/sub-template-python/Dockerfile -t sensorhub-sub-example .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PYTHONUNBUFFERED=1

COPY common/python /opt/sensorhub/sdk
RUN pip install --no-cache-dir /opt/sensorhub/sdk

COPY sub_client/sub-template-python /app
WORKDIR /app
ENTRYPOINT ["python3", "/app/sub_example.py"]
