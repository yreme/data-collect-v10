# pub-mock：无硬件模拟全部传感器
# 构建（上下文为 src/）：docker build -f pub_server/pub-mock/Dockerfile -t sensorhub-pub-mock .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PYTHONUNBUFFERED=1

COPY common/python /opt/sensorhub/sdk
RUN pip install --no-cache-dir /opt/sensorhub/sdk

COPY pub_server/pub-mock /app
WORKDIR /app
ENTRYPOINT ["python3", "/app/pub_mock.py"]
