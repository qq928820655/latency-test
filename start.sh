#!/bin/bash
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"
PORT="${LAT_PORT:-8005}"
ss -tlnp 2>/dev/null | grep -q ":${PORT} " && { echo "端口 ${PORT} 已被占用"; exit 1; }
setsid nohup python3 server.py "${PORT}" > server.log 2>&1 &
echo "latency-test 已启动, pid=$!, port=${PORT}"
