#!/usr/bin/env bash
# 一条本地命令：初始化 SQLite（导入正常和异常串码）并启动服务。
# 用法：./run.sh            默认 http://127.0.0.1:8000
#       ./run.sh --reset    清空 data/ 后重新导入
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
DB="data/app.db"
PHOTOS="data/photos"

if [[ "${1:-}" == "--reset" ]]; then
  rm -rf data
fi

if [[ ! -f "$DB" ]]; then
  python3 -m app.cli init --db "$DB" --photos "$PHOTOS"
fi

echo "------------------------------------------------------------"
echo " 门店卷烟条包流转追溯系统  →  http://127.0.0.1:${PORT}"
echo " 店员 clerk/clerk123   店长 manager/mgr123   二店 clerk2/clerk123"
echo "------------------------------------------------------------"
exec python3 -m app.cli serve --db "$DB" --photos "$PHOTOS" --port "$PORT"
