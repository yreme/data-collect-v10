#pragma once

#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace lidar_shm {

constexpr uint32_t MAGIC = 0x4C494441;  // 'LIDA'
constexpr uint32_t VERSION = 1;
constexpr size_t HEADER_SIZE = 128;
constexpr size_t META_SIZE = 64;
constexpr uint32_t ENCODING_XYZI = 2;

#pragma pack(push, 1)
struct Meta {
  uint64_t seq;
  uint32_t nbytes;
  uint32_t point_count;
  uint32_t width;
  uint32_t encoding;
  uint64_t trigger_ms;
  uint64_t recv_ms;
  uint64_t host_ms;
  uint32_t frame_num;
  uint32_t pad;
};
#pragma pack(pop)

inline size_t total_size(int slot_count, int slot_capacity) {
  return HEADER_SIZE + slot_count * META_SIZE + slot_count * slot_capacity;
}

class ShmWriter {
 public:
  ShmWriter(const std::string& name, int slot_count, int slot_capacity)
      : name_(name), slot_count_(slot_count), slot_capacity_(slot_capacity) {
    size_t size = total_size(slot_count, slot_capacity);
    shm_unlink(("/" + name).c_str());
    fd_ = shm_open(name.c_str(), O_CREAT | O_RDWR, 0666);
    if (fd_ < 0) throw std::runtime_error("shm_open failed");
    if (ftruncate(fd_, size) != 0) throw std::runtime_error("ftruncate failed");
    buf_ = static_cast<uint8_t*>(mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, 0));
    if (buf_ == MAP_FAILED) throw std::runtime_error("mmap failed");
    size_ = size;
    std::memset(buf_, 0, HEADER_SIZE);
    *reinterpret_cast<uint32_t*>(buf_) = MAGIC;
    *reinterpret_cast<uint32_t*>(buf_ + 4) = VERSION;
    *reinterpret_cast<uint32_t*>(buf_ + 8) = static_cast<uint32_t>(slot_count);
    *reinterpret_cast<uint32_t*>(buf_ + 12) = static_cast<uint32_t>(slot_capacity);
    *reinterpret_cast<uint32_t*>(buf_ + 16) = static_cast<uint32_t>(META_SIZE);
    write_seq_ = reinterpret_cast<uint64_t*>(buf_ + 64);
    *write_seq_ = 0;
    meta_base_ = HEADER_SIZE;
    data_base_ = HEADER_SIZE + slot_count * META_SIZE;
  }

  ~ShmWriter() {
    if (buf_ && buf_ != MAP_FAILED) munmap(buf_, size_);
    if (fd_ >= 0) close(fd_);
  }

  uint64_t publish(const uint8_t* data, uint32_t nbytes, uint32_t point_count,
                   uint64_t trigger_ms, uint64_t recv_ms, uint32_t frame_num) {
    if (static_cast<int>(nbytes) > slot_capacity_) return *write_seq_;
    uint64_t idx = *write_seq_;
    int slot = idx % slot_count_;
    size_t doff = data_base_ + slot * slot_capacity_;
    std::memcpy(buf_ + doff, data, nbytes);
    Meta* m = reinterpret_cast<Meta*>(buf_ + meta_base_ + slot * META_SIZE);
    m->seq = idx;
    m->nbytes = nbytes;
    m->point_count = point_count;
    m->width = point_count;
    m->encoding = ENCODING_XYZI;
    m->trigger_ms = trigger_ms;
    m->recv_ms = recv_ms;
    m->host_ms = recv_ms;
    m->frame_num = frame_num;
    (*write_seq_)++;
    return idx;
  }

 private:
  std::string name_;
  int slot_count_;
  int slot_capacity_;
  int fd_{-1};
  uint8_t* buf_{nullptr};
  size_t size_{0};
  uint64_t* write_seq_{nullptr};
  size_t meta_base_{0};
  size_t data_base_{0};
};

}  // namespace lidar_shm
