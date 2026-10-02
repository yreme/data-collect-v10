# lidar-capture: RoboSense rs_driver -> shared memory (no ROS)

cmake_minimum_required(VERSION 3.10)
project(lidar_capture LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(RS_DRIVER_DIR "${CMAKE_SOURCE_DIR}/../../sdk/rslidar_sdk-main/src/rs_driver" CACHE PATH "rs_driver path")
if(DEFINED ENV{RS_DRIVER_DIR})
  set(RS_DRIVER_DIR "$ENV{RS_DRIVER_DIR}")
endif()
if(NOT EXISTS "${RS_DRIVER_DIR}/CMakeLists.txt")
  message(FATAL_ERROR "rs_driver not found at ${RS_DRIVER_DIR}")
endif()

# rs_driver is header-only; bring in its CMake for EXTERNAL_LIBS (pthread, pcap)
set(COMPILE_DEMOS OFF CACHE BOOL "" FORCE)
add_subdirectory(${RS_DRIVER_DIR} rs_driver_build EXCLUDE_FROM_ALL)

add_executable(lidar-capture
  lidar_capture_main.cpp
  shm_writer.cpp
)

target_include_directories(lidar-capture PRIVATE
  ${RS_DRIVER_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}
)

target_link_libraries(lidar-capture PRIVATE
  pthread
  pcap
  rt
)

# rs_driver headers use uint16_t without always including <cstdint>
target_compile_options(lidar-capture PRIVATE -include cstdint)

install(TARGETS lidar-capture RUNTIME DESTINATION bin)
