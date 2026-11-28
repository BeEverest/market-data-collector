#!/bin/sh
# 容器入口：只做目录准备与进程启动，绝不清理/覆盖任何采集原数据。
set -e

PORT="${PORT:-8000}"

# 数据卷挂载点：宿主机 ./data -> 容器 /app/output
mkdir -p /app/output/monitor /app/output/exports

# 若用户把迁移包里的 market_data.db 预先放进 ./data，下面这行不会碰它；
# 只有数据卷完全为空时，系统才会按自身初始化逻辑新建空库。
echo "[entrypoint] 数据目录 /app/output，监听端口 ${PORT}，时区 ${TZ:-Asia/Shanghai}"

exec python3.11 -u /app/web_app.py --host 0.0.0.0 --port "${PORT}"
