#!/bin/sh
set -e

cd /app/web
exec python3 -m mcap_web_viz.main
