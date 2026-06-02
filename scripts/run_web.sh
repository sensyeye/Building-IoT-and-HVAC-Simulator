#!/usr/bin/env bash
# Detached uvicorn launcher for the Sensgreen Sensor Simulator UI.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p /tmp
nohup .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000 \
  > /tmp/sensgreen-uvicorn.log 2>&1 &
disown
echo "uvicorn pid=$!"
echo "log: /tmp/sensgreen-uvicorn.log"
