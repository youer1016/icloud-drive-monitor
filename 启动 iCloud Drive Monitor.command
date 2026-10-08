#!/bin/zsh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$(command -v python3)"
EVICT_SOURCE="$APP_DIR/evict_local.m"
EVICT_BINARY="$APP_DIR/evict_local"

if [[ ! -x "$EVICT_BINARY" || "$EVICT_SOURCE" -nt "$EVICT_BINARY" ]]; then
  echo "正在编译本机 iCloud 移除接口…"
  xcrun clang -fobjc-arc -framework Foundation "$EVICT_SOURCE" -o "$EVICT_BINARY"
fi

echo "iCloud Drive Monitor 将启动本机服务；监听与扫描只读，移除本地下载须在页面中另行确认。"
echo "浏览器页面打开时才会监听 Finder 可见的 iCloud Drive 路径；关闭页面后监听即停止。"
echo "系统将要求输入本机管理员密码，用于运行 fs_usage 和受控的本地移除操作。"
sudo -v
LOG_FILE="$APP_DIR/monitor.log"
OLD_PID="$(sudo lsof -nP -tiTCP:8766 -sTCP:LISTEN | head -n 1 || true)"
if [[ -n "$OLD_PID" ]]; then
  OLD_COMMAND="$(ps -p "$OLD_PID" -o command=)"
  if [[ "$OLD_COMMAND" != *"$APP_DIR/monitor_server.py"* ]]; then
    echo "端口 8766 正由其他程序占用：$OLD_COMMAND"
    exit 1
  fi
  echo "正在结束此前启动的 Monitor 服务（PID $OLD_PID）…"
  sudo kill -TERM "$OLD_PID"
  for _attempt in {1..20}; do
    if ! sudo lsof -nP -tiTCP:8766 -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done
fi
: > "$LOG_FILE"
sudo -b "$PYTHON_BIN" "$APP_DIR/monitor_server.py" > "$LOG_FILE" 2>&1

for _attempt in {1..12}; do
  if curl -fsS --max-time 1 "http://127.0.0.1:8766/api/status" >/dev/null 2>&1; then
    open "http://127.0.0.1:8766"
    echo "Monitor 已启动，浏览器页面已打开。"
    exit 0
  fi
  sleep 0.25
done

echo "Monitor 未能启动。诊断日志：$LOG_FILE"
cat "$LOG_FILE"
exit 1
