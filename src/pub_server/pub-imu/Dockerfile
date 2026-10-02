# pub-imu：Yesense IMU 采集
# 构建（上下文为 src/）：docker build -f pub_server/pub-imu/Dockerfile -t sensorhub-pub-imu .
ARG BASE_IMAGE=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:server-imu-sync-v7.1.0-x64
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PYTHONUNBUFFERED=1

COPY common/python /opt/sensorhub/sdk
RUN pip install --no-cache-dir /opt/sensorhub/sdk

COPY pub_server/pub-imu /app
WORKDIR /app
ENTRYPOINT ["python3", "/app/pub_imu.py"]
