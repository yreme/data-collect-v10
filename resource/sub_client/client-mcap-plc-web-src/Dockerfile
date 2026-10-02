# 基于 MCAP Web 镜像，集成 PLC 触发式多模态采集
# 构建（在仓库根目录）:
#   docker compose -f client-mcap-plc-web-src/docker-compose.yml build
ARG BASE_IMAGE=registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:client-mcap-web-viz-v6.8.6b-x64
FROM ${BASE_IMAGE}

WORKDIR /app

COPY multimodal_src/multimodal_sync /app/multimodal_sync
COPY multimodal_src/pyproject.toml /app/pyproject.toml
COPY client-mcap-plc-web-src/web /app/web
COPY client-mcap-plc-web-src/plc /app/plc
COPY client-mcap-plc-web-src/configs /app/configs
COPY client-mcap-plc-web-src/tools /app/tools
COPY client-mcap-plc-web-src/docker/install-deps.sh /tmp/install-deps.sh
COPY client-mcap-plc-web-src/web/requirements.txt /tmp/web-requirements.txt

RUN pip install --no-cache-dir -e /app \
    && chmod +x /tmp/install-deps.sh \
    && /tmp/install-deps.sh /tmp/web-requirements.txt

RUN mkdir -p /data/mcap/hourly /data/mcap/short /data/mcap/imu_motion

ENV PYTHONPATH=/app:/app/web \
    MCAP_WEB_CONFIG=/app/configs/client_mcap.yaml \
    MCAP_HOURLY_DIR=/data/mcap/hourly \
    MCAP_SHORT_DIR=/data/mcap/short \
    MCAP_ENABLED=true \
    MCAP_DEDUP_ENABLED=true \
    MCAP_SENSOR_APPEND_ON_UPDATE=false \
    AUTO_START=true \
    MOCK_MODE=false \
    PYTHONUNBUFFERED=1

COPY client-mcap-plc-web-src/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 18881 12730/udp
ENTRYPOINT ["/entrypoint.sh"]
