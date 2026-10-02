// sensorhub FrameHeader v1 —— 与 src/common/python/sensorhub/header.py 字节级一致（64B，小端）。
// 放在 Zenoh sample 的 attachment 中；payload 只放原始数据（可走 SHM 零拷贝）。
#pragma once

#include <chrono>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>

namespace sensorhub {

enum class Kind : uint8_t { Unknown = 0, Camera = 1, Lidar = 2, Imu = 3, Plc = 4, Gnss = 5, App = 10 };

enum class Encoding : uint8_t {
  Unknown = 0,
  Mono8 = 1, Bgr8 = 2, Rgb8 = 3,
  BayerRggb8 = 4, BayerBggr8 = 5, BayerGbrg8 = 6, BayerGrbg8 = 7, Mono16 = 8,
  Jpeg = 10, Png = 11,
  XyziF32 = 20,    // 16B/点
  XyzirtF32 = 21,  // 24B/点：x,y,z,intensity(f32) ring(u16) pad(u16) t_off_s(f32)
  ImuV1 = 30,      // 56B/样本
  Json = 40, Cbor = 41, Protobuf = 42,
};

enum Flags : uint32_t {
  kSyncHwTrigger = 1u << 0,
  kSyncActionCmd = 1u << 1,
  kSyncSoftTrigger = 1u << 2,
  kSyncPhaseLock = 1u << 3,
  kClockPtp = 1u << 4,
  kClockHost = 1u << 5,
  kGapBefore = 1u << 6,
  kMock = 1u << 7,
};

#pragma pack(push, 1)
struct FrameHeader {
  char magic[4] = {'S', 'H', 'F', '1'};
  uint16_t version = 1;
  uint8_t kind = 0;
  uint8_t encoding = 0;
  uint64_t seq = 0;
  int64_t trigger_ns = 0;  // 同步网格时刻
  int64_t stamp_ns = 0;    // 传感器时刻
  int64_t pub_ns = 0;      // 发布时刻
  uint32_t width = 0;
  uint32_t height = 0;
  uint32_t step = 0;
  uint32_t count = 0;
  uint32_t flags = 0;
  uint32_t reserved = 0;
};

struct PointXYZIRT {
  float x, y, z, intensity;
  uint16_t ring;
  uint16_t pad;
  float t;  // 相对 stamp_ns 的秒偏移
};

struct ImuSampleV1 {
  int64_t stamp_ns;
  float ax, ay, az, gx, gy, gz;
  float qw, qx, qy, qz;
  float temp_c;
  uint32_t status;
};
#pragma pack(pop)

static_assert(sizeof(FrameHeader) == 64, "FrameHeader must be 64 bytes");
static_assert(sizeof(PointXYZIRT) == 24, "PointXYZIRT must be 24 bytes");
static_assert(sizeof(ImuSampleV1) == 56, "ImuSampleV1 must be 56 bytes");

inline int64_t now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::system_clock::now().time_since_epoch()).count();
}

inline FrameHeader parse_header(const uint8_t* data, size_t len) {
  if (len < sizeof(FrameHeader)) throw std::runtime_error("FrameHeader too short");
  FrameHeader h;
  std::memcpy(&h, data, sizeof(FrameHeader));
  if (std::memcmp(h.magic, "SHF1", 4) != 0) throw std::runtime_error("FrameHeader bad magic");
  return h;
}

// ---- 同步网格（与 sync_grid.py 一致）----
inline int64_t align_up(int64_t t_ns, int64_t period_ns, int64_t phase_ns = 0) {
  int64_t d = t_ns - phase_ns;
  int64_t k = d >= 0 ? (d + period_ns - 1) / period_ns : -((-d) / period_ns);
  return k * period_ns + phase_ns;
}

inline int64_t nearest_slot(int64_t t_ns, int64_t period_ns, int64_t phase_ns = 0) {
  int64_t d = t_ns - phase_ns;
  int64_t lo = (d >= 0 ? d / period_ns : -((-d + period_ns - 1) / period_ns)) * period_ns + phase_ns;
  return (t_ns - lo) * 2 < period_ns ? lo : lo + period_ns;
}

// ---- key 命名（与 keys.py 一致）----
inline std::string sensor_key(const std::string& prefix, const std::string& kind,
                              const std::string& name, const std::string& channel) {
  return prefix + "/" + kind + "/" + name + "/" + channel;
}

}  // namespace sensorhub
