/*********************************************************************************************************************
 * lidar-capture: RoboSense rs_driver -> stdout frames -> Python SHM bridge
 *********************************************************************************************************************/

#include <cstdint>
#include <rs_driver/api/lidar_driver.hpp>
#include <rs_driver/msg/point_cloud_msg.hpp>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <vector>

using namespace robosense::lidar;

typedef PointXYZI PointT;
typedef PointCloudT<PointT> PointCloudMsg;

struct Options {
  uint16_t msop_port = 6699;
  uint16_t difop_port = 7788;
  std::string lidar_type = "RSE1";
  std::string host_address = "0.0.0.0";
  std::string group_address = "0.0.0.0";
  int hz = 10;
};

static Options parse_args(int argc, char** argv) {
  Options o;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&](std::string& dst) {
      if (i + 1 < argc) dst = argv[++i];
    };
    if (a == "--shm" || a == "--slot-count" || a == "--slot-capacity") {
      if (i + 1 < argc) ++i;  // legacy args ignored
    } else if (a == "--msop-port") o.msop_port = static_cast<uint16_t>(std::atoi(argv[++i]));
    else if (a == "--difop-port") o.difop_port = static_cast<uint16_t>(std::atoi(argv[++i]));
    else if (a == "--lidar-type") next(o.lidar_type);
    else if (a == "--host-address") next(o.host_address);
    else if (a == "--group-address") next(o.group_address);
    else if (a == "--lidar-ip") { if (i + 1 < argc) ++i; }
    else if (a == "--hz") o.hz = std::atoi(argv[++i]);
    else if (a == "--help" || a == "-h") {
      std::fprintf(stderr,
                   "Usage: lidar-capture --msop-port 6699 --difop-port 7788 "
                   "[--lidar-type RSE1] [--host-address 0.0.0.0] "
                   "[--group-address 224.0.0.205] [--hz 10]\n");
      std::exit(0);
    }
  }
  return o;
}

static SyncQueue<std::shared_ptr<PointCloudMsg>> free_q;
static SyncQueue<std::shared_ptr<PointCloudMsg>> stuffed_q;
static std::atomic<bool> to_exit{false};

static std::shared_ptr<PointCloudMsg> getCloud() {
  auto msg = free_q.pop();
  return msg ? msg : std::make_shared<PointCloudMsg>();
}

static void putCloud(std::shared_ptr<PointCloudMsg> msg) {
  stuffed_q.push(msg);
}

static void exceptionCb(const Error& e) {
  std::fprintf(stderr, "WARNING %s\n", e.toString().c_str());
}

static std::vector<uint8_t> pack_points(const PointCloudMsg& cloud) {
  const size_t n = cloud.points.size();
  std::vector<uint8_t> buf(n * 16);
  float* f = reinterpret_cast<float*>(buf.data());
  for (size_t i = 0; i < n; ++i) {
    const auto& p = cloud.points[i];
    f[i * 4 + 0] = p.x;
    f[i * 4 + 1] = p.y;
    f[i * 4 + 2] = p.z;
    f[i * 4 + 3] = static_cast<float>(p.intensity);
  }
  return buf;
}

static void emit_frame(uint32_t seq, uint32_t npts, const std::vector<uint8_t>& packed,
                       uint64_t trigger_ms) {
  const uint32_t nbytes = static_cast<uint32_t>(packed.size());
  std::printf("FRAME %u %u %u %llu\n", seq, npts, nbytes,
              static_cast<unsigned long long>(trigger_ms));
  if (nbytes > 0) {
    std::fwrite(packed.data(), 1, nbytes, stdout);
  }
  std::fflush(stdout);
  std::fprintf(stderr, "PUBLISH seq=%u points=%u\n", seq, npts);
}

int main(int argc, char** argv) {
  Options opt = parse_args(argc, argv);
  std::fprintf(stderr,
               "lidar-capture msop=%u difop=%u type=%s host=%s group=%s hz=%d (stdout bridge)\n",
               opt.msop_port, opt.difop_port, opt.lidar_type.c_str(),
               opt.host_address.c_str(), opt.group_address.c_str(), opt.hz);

  RSDriverParam param;
  param.input_type = InputType::ONLINE_LIDAR;
  param.input_param.msop_port = opt.msop_port;
  param.input_param.difop_port = opt.difop_port;
  param.input_param.host_address = opt.host_address;
  param.input_param.group_address = opt.group_address;
  param.lidar_type = strToLidarType(opt.lidar_type);
  param.decoder_param.wait_for_difop = true;
  param.decoder_param.ts_first_point = true;

  LidarDriver<PointCloudMsg> driver;
  driver.regPointCloudCallback(getCloud, putCloud);
  driver.regExceptionCallback(exceptionCb);

  if (!driver.init(param)) {
    std::fprintf(stderr, "ERROR Driver init failed\n");
    return 1;
  }

  const int period_ms = std::max(1, 1000 / std::max(1, opt.hz));
  auto last_pub = std::chrono::steady_clock::now();

  std::thread proc([&]() {
    while (!to_exit) {
      auto msg = stuffed_q.popWait(500);
      if (!msg) continue;

      auto now = std::chrono::steady_clock::now();
      auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_pub).count();
      if (elapsed < period_ms) {
        free_q.push(msg);
        continue;
      }
      last_pub = now;

      try {
        auto packed = pack_points(*msg);
        auto wall_ms = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count());
        uint32_t npts = static_cast<uint32_t>(msg->points.size());
        emit_frame(msg->seq, npts, packed, wall_ms);
      } catch (const std::exception& e) {
        std::fprintf(stderr, "ERROR publish: %s\n", e.what());
      }
      free_q.push(msg);
    }
  });

  driver.start();
  std::fprintf(stderr, "Driver started, publishing @ %dHz via stdout bridge\n", opt.hz);

  while (true) {
    std::this_thread::sleep_for(std::chrono::seconds(3600));
  }

  to_exit = true;
  driver.stop();
  proc.join();
  return 0;
}
