# pub-plc：727R 起重机 PLC UDP 采集
# 构建（上下文为 src/）：docker build -f pub_server/pub-plc/Dockerfile -t sensorhub-pub-plc .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PYTHONUNBUFFERED=1

COPY common/python /opt/sensorhub/sdk
RUN pip install --no-cache-dir /opt/sensorhub/sdk

COPY pub_server/pub-plc /app
WORKDIR /app
ENTRYPOINT ["python3", "/app/pub_plc.py"]
