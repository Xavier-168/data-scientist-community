#!/usr/bin/env bash
# 数据科学家 Community - WSL 一键启动/停止脚本
# 用法: ./wsl_start.sh [start|stop|status|restart]   (默认 start)
# start: 启动服务（已运行则跳过），打印访问地址，并在 Windows 侧打开浏览器

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-8811}"
ACTION="${1:-start}"
STATE_AUTH="$HOME/.local/share/data-scientist-community/.auth/session.json"

is_running() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/monitor" 2>/dev/null; }

case "$ACTION" in
  stop)
    fuser -k "$PORT/tcp" 2>/dev/null && echo "[ok] 已停止" || echo "[info] 服务未在运行"
    exit 0
    ;;
  status)
    is_running && echo "[ok] 运行中 (http://127.0.0.1:$PORT/monitor)" || echo "[info] 未运行"
    exit 0
    ;;
  restart)
    fuser -k "$PORT/tcp" 2>/dev/null || true
    sleep 1
    ;;
  start) ;;
  *)
    echo "用法: $0 [start|stop|status|restart]"
    exit 1
    ;;
esac

# 启动（start.sh 会检测已运行的 healthy runner 并直接退出）
if ! is_running; then
  echo "[info] 启动服务..."
  ./start.sh
else
  echo "[info] 服务已在运行"
fi

# 等待就绪
for _ in $(seq 1 30); do
  is_running && break
  sleep 1
done
if ! is_running; then
  echo "[error] 服务启动失败，请查看上方日志"
  exit 1
fi

WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SESSION="$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('$STATE_AUTH')))['token'])" 2>/dev/null || true)"
URL="http://${WSL_IP:-127.0.0.1}:$PORT/monitor"

echo ""
echo "  ┌─────────────────────────────────────────────┐"
echo "  │  本地 (WSL 内):   http://127.0.0.1:$PORT/monitor   │"
echo "  │  Windows 侧:     $URL"
echo "  │  会话 token:     ${SESSION:0:12}...（页面自动携带）   │"
echo "  └─────────────────────────────────────────────┘"
echo ""

# 在 Windows 侧打开默认浏览器（WSL2 互操作；explorer.exe 不阻塞）
CMD_EXE="$(command -v cmd.exe || echo /mnt/c/Windows/System32/cmd.exe)"
INTEROP_SOCK="$(ls /run/WSL/*_interop 2>/dev/null | head -1 || true)"
if [[ -n "${WSL_INTEROP:-}" || -n "$INTEROP_SOCK" ]] && [[ -x "$CMD_EXE" ]]; then
  EXPLORER="$(command -v explorer.exe || echo /mnt/c/Windows/explorer.exe)"
  echo "[info] 正在 Windows 侧打开浏览器..."
  if [[ -n "$SESSION" ]]; then
    "$EXPLORER" "$URL#session=$SESSION" >/dev/null 2>&1 &
  else
    "$EXPLORER" "$URL" >/dev/null 2>&1 &
  fi
  disown 2>/dev/null || true
else
  echo "[info] Windows 互操作不可用，请手动访问: $URL"
fi
