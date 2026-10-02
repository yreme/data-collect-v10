FROM registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:client-mcap-web-viz-v6.8.6b-x64

WORKDIR /app/plc-collect

COPY python/requirements.txt /app/plc-collect/python/requirements.txt
COPY docker/install-deps.sh /tmp/install-deps.sh
RUN chmod +x /tmp/install-deps.sh && /tmp/install-deps.sh /app/plc-collect/python/requirements.txt

COPY python/ /app/plc-collect/python/
COPY foxglove/ /app/plc-collect/foxglove/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /data/mcap/hourly /data/mcap/short

ENV MCAP_HOURLY_DIR=/data/mcap/hourly \
    MCAP_SHORT_DIR=/data/mcap/short \
    MCAP_HOURLY_ROTATION_SECONDS=3600 \
    MCAP_SHORT_ROTATION_SECONDS=600 \
    MCAP_HOURLY_RETENTION_DAYS=30 \
    MCAP_SHORT_RETENTION_DAYS=1 \
    MCAP_ENABLED=true \
    MOCK_MODE=false \
    HEALTH_TIMEOUT=5 \
    PYTHONUNBUFFERED=1

EXPOSE 8080 12730/udp

WORKDIR /app/plc-collect/python
ENTRYPOINT ["/entrypoint.sh"]
