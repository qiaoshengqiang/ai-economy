#!/usr/bin/env bash
# ============================================================
# AIEOS 启动脚本
# ------------------------------------------------------------
# 在项目根目录后台启动 / 停止 / 重启 UI 服务。
#
#   ./start_aieos.sh start
#   ./start_aieos.sh stop
#   ./start_aieos.sh restart
#   ./start_aieos.sh status
#
# 默认：
#   port      8000
#   pi agents agent_c
#   pi CLI     experiments/pi_compare/pi_node/node_modules/.bin/pi
#
# 可用环境变量覆盖：
#   AIEOS_PORT / AIEOS_PI_AGENTS / AIEOS_PI_CLI
#   AIEOS_LOG / AIEOS_PID_FILE
# ============================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${AIEOS_PORT:-8000}"
PI_AGENTS="${AIEOS_PI_AGENTS:-agent_c}"
PI_CLI="${AIEOS_PI_CLI:-$ROOT/experiments/pi_compare/pi_node/node_modules/.bin/pi}"
LOG="${AIEOS_LOG:-$ROOT/runs/ui/server.log}"
PID_FILE="${AIEOS_PID_FILE:-$ROOT/runs/ui/aieos.pid}"
PYTHON="$ROOT/.venv/bin/python"
URL="http://127.0.0.1:${PORT}"

mkdir -p "$(dirname "$LOG")" "$(dirname "$PID_FILE")"

fail() {
  echo "❌ $*" >&2
  exit 1
}

running_pid() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null \
        && ps -p "$pid" -o command= 2>/dev/null | grep -q "ui.server"; then
      echo "$pid"
      return 0
    fi
    rm -f "$PID_FILE"
  fi

  local pid
  pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  if [ -n "$pid" ] && ps -p "$pid" -o command= 2>/dev/null | grep -q "ui.server"; then
    echo "$pid"
    return 0
  fi
  return 1
}

require_runtime() {
  [ -x "$PYTHON" ] || fail "找不到虚拟环境 Python：$PYTHON

请先准备环境：
  python3 -m venv .venv
  ./.venv/bin/python -m pip install -r requirements.txt"

  [ -f "$ROOT/.env" ] || echo "⚠️  没有找到 .env；如果模型 Key 未在系统环境中配置，启动后调用会失败。"

  [ -x "$PI_CLI" ] || fail "找不到 Pi CLI：$PI_CLI

如果你不使用 Pi Agent，或 Pi 安装在其他位置，可以运行：
  AIEOS_PI_CLI=/path/to/pi ./start_aieos.sh start"
}

health_ok() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS "$URL/api/health" >/dev/null 2>&1
}

start_service() {
  require_runtime

  local pid
  if pid="$(running_pid)"; then
    echo "AIEOS 已经在运行（PID $pid）"
    echo "  $URL"
    exit 0
  fi

  local port_pid
  port_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  if [ -n "$port_pid" ]; then
    fail "端口 $PORT 已被其他进程占用（PID $port_pid）。
先确认它是什么进程，或换端口：
  AIEOS_PORT=8001 ./start_aieos.sh start"
  fi

  echo "▶ 启动 AIEOS..."
  echo "  port:      $PORT"
  echo "  Pi agents: $PI_AGENTS"
  echo "  Pi CLI:    $PI_CLI"
  echo "  log:       $LOG"

  nohup "$PYTHON" -m ui.server \
    --port "$PORT" \
    --pi-agents "$PI_AGENTS" \
    --pi-cli "$PI_CLI" \
    >"$LOG" 2>&1 &
  pid=$!
  echo "$pid" >"$PID_FILE"

  local ready=0
  for _ in $(seq 1 24); do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    if health_ok; then
      ready=1
      break
    fi
    sleep 0.5
  done

  if [ "$ready" -ne 1 ]; then
    echo "❌ 启动失败；最近日志：" >&2
    tail -n 30 "$LOG" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi

  echo "✅ AIEOS 已启动"
  echo "  $URL"
  echo "  PID:       $pid"
  echo "  log:       $LOG"
}

stop_service() {
  local pid
  if ! pid="$(running_pid)"; then
    echo "AIEOS 当前没有运行。"
    rm -f "$PID_FILE"
    return 0
  fi

  echo "▶ 停止 AIEOS（PID $pid）..."
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "  ⚠️  进程未在 10 秒内退出，强制结束。"
    kill -9 "$pid" 2>/dev/null || true
  fi

  rm -f "$PID_FILE"
  echo "✅ 已停止"
}

status_service() {
  local pid
  if pid="$(running_pid)"; then
    echo "AIEOS 正在运行"
    echo "  URL: $URL"
    echo "  PID: $pid"
    if health_ok; then
      echo "  健康检查: OK"
    else
      echo "  健康检查: 未通过"
    fi
  else
    echo "AIEOS 当前没有运行。"
    return 1
  fi
}

case "${1:-start}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  status)
    status_service
    ;;
  *)
    echo "用法：$0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
