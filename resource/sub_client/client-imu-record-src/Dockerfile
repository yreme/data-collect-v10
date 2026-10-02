# IMU 专用录制：共享内存 → MCAP + CSV（6 小时整点切分）
FROM registry.cn-beijing.aliyuncs.com/useful-tools/0-tmp-image:client-foxglove-sync-v6.6.24-x64

WORKDIR /app

# 覆盖 multimodal_sync（含更新的 shm_discovery）
COPY pyproject.toml /app/
COPY multimodal_sync /app/multimodal_sync
RUN pip install --no-cache-dir --no-deps -e /app || true

WORKDIR /app/web
COPY client-imu-record/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY client-imu-record/imu_record ./imu_record

ENV IMU_RECORD_HOST=0.0.0.0
ENV IMU_RECORD_PORT=18882
ENV IMU_RECORD_CONFIG=/app/configs/client_imu_record.yaml
ENV IMU_RECORD_OUTPUT_DIR=/data/imu
ENV IMU_RECORD_DEFAULT_PREFIX=imu
ENV IMU_RECORD_PERIOD_HOURS=6
ENV IMU_RECORD_AUTO_START=1
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app:/app/web

EXPOSE 18882

ENTRYPOINT []
CMD ["python3", "-m", "imu_record.main"]
