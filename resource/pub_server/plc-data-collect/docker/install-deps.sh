#!/bin/sh
# Install Python deps only when missing (base image may already include mcap SDK).
set -e

REQ_FILE="${1:-/app/plc-collect/python/requirements.txt}"

python3 - <<'PY' "$REQ_FILE"
import importlib.util
import subprocess
import sys
from pathlib import Path

req_file = Path(sys.argv[1])
missing = []
for line in req_file.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name = line.split(">=")[0].split("==")[0].split("[")[0].strip()
    module = {
        "mcap-protobuf-support": "mcap_protobuf",
        "foxglove-schemas-protobuf": "foxglove_schemas_protobuf",
        "uvicorn": "uvicorn",
    }.get(name, name.replace("-", "_"))
    if importlib.util.find_spec(module) is None:
        missing.append(line)

if not missing:
    print("All Python dependencies already present in base image.")
    sys.exit(0)

print("Installing missing packages:", ", ".join(missing))
subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "--no-cache-dir", *missing]
)
PY
