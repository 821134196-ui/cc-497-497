#!/usr/bin/env bash
# 一条本地命令：初始化 SQLite、导入正常与异常演示串码、启动 Web 服务。
# 用法：./start.sh [端口]   默认端口 8000
set -euo pipefail

cd "$(dirname "$0")"
PORT="${1:-8000}"
DB="data/app.db"

rm -f "$DB" "$DB-wal" "$DB-shm"
python3 -m traceapp.seed --db "$DB" --photos data/photos
echo "------------------------------------------------------------"
echo "服务启动中：http://127.0.0.1:${PORT}"
echo "账号：zhang(店员) / wang(店长) ；S002：zhao(店员) / qian(店长)"
echo "------------------------------------------------------------"
exec python3 -m traceapp.app --db "$DB" --photos data/photos --port "$PORT"
