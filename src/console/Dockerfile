# sensorhub 控制台镜像
# 构建（上下文为 src/）：docker build -f console/Dockerfile -t sensorhub-console .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PYTHONUNBUFFERED=1

COPY console/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY common/python /opt/sensorhub/sdk
RUN pip install --no-cache-dir --no-deps /opt/sensorhub/sdk

COPY console/sensorhub_console /app/sensorhub_console
WORKDIR /app
EXPOSE 18100
ENTRYPOINT ["python3", "-m", "sensorhub_console"]
