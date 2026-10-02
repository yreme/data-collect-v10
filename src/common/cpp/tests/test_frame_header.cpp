// g++ -std=c++17 -I.. test_frame_header.cpp -o /tmp/t && /tmp/t <hex-from-python>
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>

#include "sensorhub/frame_header.hpp"

int main(int argc, char** argv) {
  using namespace sensorhub;
  FrameHeader h;
  h.kind = static_cast<uint8_t>(Kind::Camera);
  h.encoding = static_cast<uint8_t>(Encoding::BayerRggb8);
  h.seq = 7;
  h.trigger_ns = 100;
  h.stamp_ns = 123;
  h.width = 1920;
  h.height = 1080;
  h.step = 1920;
  h.count = 1;
  h.flags = 3;

  const auto* p = reinterpret_cast<const uint8_t*>(&h);
  std::string hex;
  char buf[3];
  for (size_t i = 0; i < sizeof(h); ++i) {
    std::snprintf(buf, sizeof(buf), "%02x", p[i]);
    hex += buf;
  }
  if (argc > 1) {
    if (hex != argv[1]) {
      std::fprintf(stderr, "MISMATCH\n cpp=%s\n py =%s\n", hex.c_str(), argv[1]);
      return 1;
    }
  }
  FrameHeader back = parse_header(p, sizeof(h));
  assert(back.seq == 7 && back.width == 1920);
  assert(align_up(1'000'000'001, 50'000'000) == 1'050'000'000);
  assert(nearest_slot(1'049'000'000, 50'000'000) == 1'050'000'000);
  assert(sensor_key("rig", "lidar", "lidar0", "points") == "rig/lidar/lidar0/points");
  std::printf("OK %s\n", hex.c_str());
  return 0;
}
