#!/usr/bin/env bash
# make demo 的本地验收脚本：迁移 → 演示数据 → 启动 API → 验证 health/papers → 停止。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BIN="${BIN:-$ROOT/.venv/bin}"
PORT="${DEMO_PORT:-8000}"

if ! command -v curl >/dev/null 2>&1; then
  echo "错误：需要 curl 才能执行演示（Docker 内使用 Python healthcheck，不受影响）" >&2
  exit 1
fi

for tool in alembic papersteward uvicorn python; do
  if [ ! -x "$BIN/$tool" ]; then
    echo "缺少 $BIN/$tool；请先运行 make bootstrap" >&2
    exit 1
  fi
done

echo ">> 迁移数据库"
"$BIN/alembic" upgrade head

echo ">> 写入演示数据"
"$BIN/papersteward" demo seed

log="$(mktemp)"
pid=""
cleanup() {
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -f "$log"
}
trap cleanup EXIT

echo ">> 启动 API：http://127.0.0.1:$PORT"
"$BIN/uvicorn" papersteward.main:app --host 127.0.0.1 --port "$PORT" >"$log" 2>&1 &
pid=$!

ready=0
for _ in $(seq 1 50); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "API 提前退出：" >&2
    cat "$log" >&2
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.2
done

if [ "$ready" -ne 1 ]; then
  echo "等待 /health 超时：" >&2
  cat "$log" >&2
  exit 1
fi

echo ">> GET /health"
curl -fsS "http://127.0.0.1:$PORT/health" | "$BIN/python" -m json.tool
echo ">> GET /papers"
curl -fsS "http://127.0.0.1:$PORT/papers?limit=10" | "$BIN/python" -m json.tool
echo ">> 演示完成（API 已停止）"
