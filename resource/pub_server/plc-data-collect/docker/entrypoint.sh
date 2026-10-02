#!/bin/sh
set -e

cd /app/plc-collect/python
WEB_PORT="${WEB_PORT:-8080}"
UDP_PORT="${UDP_PORT:-12730}"

exec python3 monitor.py --host 0.0.0.0 --port "$WEB_PORT" --listen-udp "$UDP_PORT" --no-mock
