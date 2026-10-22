#!/usr/bin/env bash
# 能用的搜索引擎 · 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")"
PORT="${PORT:-18788}"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[X] 没有找到 Python。请先装 3.10 或更高版本：https://www.python.org/downloads/"
  exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "[X] Python 版本太旧，需要 3.10 或更高。当前：$("$PY" -V 2>&1)"
  exit 1
fi

export PYTHONUTF8=1
export PYTHONPATH=src

echo "[1/2] $("$PY" -V 2>&1) 已就绪（本项目不装任何第三方库）"
echo "[2/2] 启动本地服务： http://127.0.0.1:${PORT}/"
echo "      按 Ctrl+C 停止。"

(
  sleep 2
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 || true
  fi
) &

exec "$PY" -m usable_search serve --port "$PORT"
