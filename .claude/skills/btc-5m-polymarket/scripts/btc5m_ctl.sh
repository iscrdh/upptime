#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$SKILL_ROOT/runtime"
RUNNER="${BTC5M_RUNNER:-$SCRIPT_DIR/btc5m_trade.py}"
ENV_FILE="${BTC5M_ENV_FILE:-$SKILL_ROOT/.env}"

# Prefer the skill's own venv when present, else system python3.
if [[ -n "${BTC5M_PYTHON:-}" ]]; then
  PY="$BTC5M_PYTHON"
elif [[ -x "$SKILL_ROOT/.venv/bin/python" ]]; then
  PY="$SKILL_ROOT/.venv/bin/python"
else
  PY="python3"
fi

PIDFILE="$RUNTIME_DIR/btc5m.pid"
METAFILE="$RUNTIME_DIR/btc5m.meta.json"
LATEST_LINK="$RUNTIME_DIR/latest.log"

mkdir -p "$RUNTIME_DIR"

usage() {
  cat <<'EOF'
Usage:
  btc5m_ctl.sh start [--execute] [--profile conservative|aggressive] [extra runner flags...]
  btc5m_ctl.sh status
  btc5m_ctl.sh stop
  btc5m_ctl.sh report [--limit N]
  btc5m_ctl.sh logs

Notes:
- start is DRY-RUN unless --execute is passed explicitly.
- Extra flags are forwarded verbatim to scripts/btc5m_trade.py
  (e.g. --stake-usd 4 --threshold 0.72 --entry-timeout-min 30).
- stop sends SIGTERM; the runner closes any open position before exiting,
  so give it time. SIGKILL is only used after a 45s grace period.
- Credentials/env are read from .env at the skill root (or BTC5M_ENV_FILE).
EOF
}

is_running() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1
  else
    return 1
  fi
}

cmd_start() {
  local profile="conservative"
  local -a extra=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile) profile="$2"; shift 2;;
      *) extra+=("$1"); shift;;
    esac
  done

  if is_running; then
    echo "already_running pid=$(cat "$PIDFILE")"
    return 0
  fi

  local ts log
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$RUNTIME_DIR/btc5m_${profile}_${ts}.log"

  (
    if [[ -f "$ENV_FILE" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "$ENV_FILE"
      set +a
    fi
    nohup "$PY" "$RUNNER" --profile "$profile" ${extra[@]+"${extra[@]}"} >"$log" 2>&1 &
    echo $! >"$PIDFILE"
  )

  ln -sfn "$log" "$LATEST_LINK"
  local pid
  pid="$(cat "$PIDFILE")"

  cat >"$METAFILE" <<JSON
{
  "startedAt": "$(date -u +%FT%TZ)",
  "pid": $pid,
  "profile": "$profile",
  "extraArgs": "${extra[*]:-}",
  "log": "$log"
}
JSON

  sleep 1
  if ps -p "$pid" >/dev/null 2>&1; then
    echo "started pid=$pid log=$log"
  else
    echo "failed_to_start (check $log)"
    exit 1
  fi
}

cmd_status() {
  if is_running; then
    local pid
    pid="$(cat "$PIDFILE")"
    echo "running pid=$pid"
    ps -p "$pid" -o pid=,etime=,command=
  else
    echo "stopped"
  fi
  [[ -f "$METAFILE" ]] && echo "meta=$METAFILE"
  [[ -L "$LATEST_LINK" ]] && echo "latest_log=$(readlink "$LATEST_LINK")"
}

cmd_stop() {
  if ! is_running; then
    echo "already_stopped"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  kill "$pid" || true
  # The runner traps SIGTERM and closes any open position before exiting.
  local waited=0
  while ps -p "$pid" >/dev/null 2>&1 && [[ $waited -lt 45 ]]; do
    sleep 1
    waited=$((waited + 1))
  done
  if ps -p "$pid" >/dev/null 2>&1; then
    echo "grace_period_expired, sending SIGKILL (position may remain open!)"
    kill -9 "$pid" || true
  fi
  rm -f "$PIDFILE"
  echo "stopped pid=$pid after ${waited}s"
}

cmd_report() {
  "$PY" "$SCRIPT_DIR/btc5m_report.py" --runtime-dir "$RUNTIME_DIR" "$@"
}

cmd_logs() {
  if [[ -L "$LATEST_LINK" ]]; then
    tail -n 120 "$(readlink "$LATEST_LINK")"
  else
    echo "no_logs"
  fi
}

main() {
  local cmd="${1:-}"
  [[ -z "$cmd" ]] && { usage; exit 2; }
  shift || true
  case "$cmd" in
    start) cmd_start "$@" ;;
    status) cmd_status ;;
    stop) cmd_stop ;;
    report) cmd_report "$@" ;;
    logs) cmd_logs ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
