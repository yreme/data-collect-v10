#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build . -j"$(nproc)"
echo "Built: $(pwd)/lidar-capture"
install -m 755 lidar-capture ../bin/lidar-capture 2>/dev/null || mkdir -p ../bin && cp lidar-capture ../bin/
