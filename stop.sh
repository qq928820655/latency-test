#!/bin/bash
DIR=$(cd "$(dirname "$0")" && pwd)
[ -f "$DIR/.env" ] && set -a && . "$DIR/.env" && set +a
PORT="${LAT_PORT:-8005}"
PID=$(ss -tlnp 2>/dev/null | grep ":${PORT} " | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)
if [ -n "$PID" ]; then kill "$PID" && echo "已停止 pid=$PID port=${PORT}"; else echo "端口 ${PORT} 无进程"; fi
