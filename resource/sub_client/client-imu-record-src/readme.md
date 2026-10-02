# IMU 共享内存录制（MCAP + CSV）

从 `server-imu-shm` 写入的 POSIX 共享内存（`imu_*`）读取 IMU 数据，同时保存：

- `imu_YYYY-MM-DD_HH-MM-SS.mcap`
- `imu_YYYY-MM-DD_HH-MM-SS.csv`

## 切分规则

按容器时区（默认 `Asia/Shanghai`）对齐到每天：

| 时刻 | 文件名时间戳 |
|------|-------------|
| 00:00–05:59 | `…_00-00-00` |
| 06:00–11:59 | `…_06-00-00` |
| 12:00–17:59 | `…_12-00-00` |
| 18:00–23:59 | `…_18-00-00` |

整点到达时关闭当前文件并打开下一时段文件。

## 启动

```bash
# 先确保 IMU server 在写 /dev/shm
cd ../server-imu-shm && docker compose up -d

cd ../client-imu-record
cp .env.example .env   # 按需修改
export IMU_OUTPUT_DIR=../output
mkdir -p ${IMU_OUTPUT_DIR}/imu
docker-compose up -d --build
```

- Web 控制台：`http://<本机IP>:18882`（默认自动开始录制）
- 数据目录：`${IMU_OUTPUT_DIR}/imu/`

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `TZ` | `Asia/Shanghai` | 整点切分所用时区 |
| `IMU_OUTPUT_DIR` | `./data` | 宿主机数据根目录（挂载到 `/data`） |
| `IMU_RECORD_PORT` | `18882` | Web 端口 |
| `IMU_RECORD_PERIOD_HOURS` | `6` | 切分周期（小时） |
| `IMU_RECORD_DEFAULT_PREFIX` | `imu` | 文件名前缀 |
| `IMU_RECORD_AUTO_START` | `1` | 容器启动后自动录制 |

## CSV 列

`imu_name, seq, trigger_ms, recv_ms, host_ms, frame_num, log_ns, tid, smp_timestamp, ready_timestamp, acc_*, gyro_*, roll/pitch/yaw, q0..q3, pos_*, vel_*, sensor_temp, status`
