#!/usr/bin/env bash
#
# Control script for the healthtech-jobs Flask server.
#
#   ./server.sh start     # start in the background (no-op if already running)
#   ./server.sh stop      # stop it
#   ./server.sh restart   # stop then start
#   ./server.sh status    # is it running?
#   ./server.sh logs      # tail -f the log
#
# Pass extra args through to server.py after the command, e.g.:
#   ./server.sh start --port 8080
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$ROOT"
PID_FILE="$ROOT/.server.pid"
LOG_FILE="$ROOT/.server.log"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-5050}"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# Kill anything currently listening on $PORT, even if this script didn't
# start it (e.g. a server launched by hand). Ensures a clean start.
free_port() {
  local pids
  pids="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0
  echo "Port $PORT in use by pid(s): $pids -> killing"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 10); do
    pids="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
    [[ -z "$pids" ]] && break
    sleep 1
  done
  pids="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  # shellcheck disable=SC2086
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
}

start() {
  # Always start clean: stop a tracked instance and free the port of any
  # other listener before launching.
  if is_running; then
    stop
  fi
  rm -f "$PID_FILE"
  free_port
  echo "Starting server... (log: $LOG_FILE)"
  cd "$APP_DIR"
  nohup "$PYTHON" server.py --port "$PORT" "$@" >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  # Give it a moment and confirm it survived boot.
  sleep 1
  if is_running; then
    echo "Started (pid $pid) -> http://127.0.0.1:$PORT/"
  else
    echo "Failed to start. Last log lines:"
    tail -n 20 "$LOG_FILE"
    rm -f "$PID_FILE"
    return 1
  fi
}

stop() {
  if ! is_running; then
    echo "Not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping (pid $pid)..."
  kill "$pid" 2>/dev/null || true
  # Wait up to 10s for a graceful exit, then force.
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "Still alive, sending SIGKILL."
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "Stopped."
}

status() {
  if is_running; then
    echo "Running (pid $(cat "$PID_FILE")) -> http://127.0.0.1:$PORT/"
  else
    echo "Not running."
    return 1
  fi
}

cmd="${1:-}"
[[ $# -gt 0 ]] && shift || true

case "$cmd" in
  start)   start "$@" ;;
  stop)    stop ;;
  restart) stop; start "$@" ;;
  status)  status ;;
  logs)    tail -f "$LOG_FILE" ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs} [extra server.py args]"
    exit 1
    ;;
esac
