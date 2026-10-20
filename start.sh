#!/usr/bin/env bash
# One launcher for the simulator and native Qt panel, from any working directory.
set -eo pipefail
demo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
scripts="$demo_root/ros2_ws/src/produce_order_workstation/scripts"
main_unit=produce-order-workstation.service
panel_unit=produce-order-panel.service

start_one() {
  local unit=$1 script=$2
  if systemctl --user is-active --quiet "$unit"; then
    echo "$unit 已在运行，不重复启动"
    return
  fi
  if [ -z "${DISPLAY:-}" ]; then
    for number in 0 1; do
      if [ -S "/tmp/.X11-unix/X$number" ]; then export DISPLAY=":$number"; break; fi
    done
  fi
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [ -z "${XAUTHORITY:-}" ] && [ -f "$XDG_RUNTIME_DIR/gdm/Xauthority" ]; then
    export XAUTHORITY="$XDG_RUNTIME_DIR/gdm/Xauthority"
  fi
  if [ -z "${DISPLAY:-}" ]; then
    echo "没有图形桌面 DISPLAY；请在已登录桌面的终端运行。" >&2
    exit 1
  fi
  systemctl --user reset-failed "$unit" 2>/dev/null || true
  local state_args=()
  if [ -n "${PRODUCE_STATE_DB:-}" ]; then
    state_args+=(--setenv="PRODUCE_STATE_DB=$PRODUCE_STATE_DB")
  fi
  systemd-run --user --collect --unit="$unit" \
    --setenv="DISPLAY=$DISPLAY" \
    --setenv="XAUTHORITY=${XAUTHORITY:-}" \
    --setenv="XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" \
    --setenv="PRODUCE_ROS_DOMAIN_ID=${PRODUCE_ROS_DOMAIN_ID:-0}" \
    "${state_args[@]}" \
    --property="WorkingDirectory=$demo_root" \
    --property=TimeoutStopSec=25 \
    /bin/bash "$script"
}

stop_all() {
  for unit in "$panel_unit" "$main_unit" produce-large-order-panel.service produce-large-order-validation.service; do
    if [ "$(systemctl --user show "$unit" -p LoadState --value 2>/dev/null)" != "not-found" ]; then
      systemctl --user stop "$unit"
    fi
  done
}

start_all() {
  for unit in produce-large-order-validation.service produce-large-order-panel.service; do
    if systemctl --user is-active --quiet "$unit"; then
      echo "旧测试服务 $unit 仍在运行。先用 bash $demo_root/start.sh stop 停止旧实例。" >&2
      exit 1
    fi
  done
  if ! systemctl --user is-active --quiet "$main_unit" && \
     pgrep -u "$(id -u)" -f 'RAIManipulationDemo[.]GameLauncher' >/dev/null; then
    echo "检测到另一套仿真进程；请先在其启动终端按 Ctrl+C，避免两套仿真争用内存。" >&2
    exit 1
  fi
  start_one "$main_unit" "$scripts/start_workstation.sh"
  start_one "$panel_unit" "$scripts/start_debug_panel.sh"
  echo "已请求启动。看日志：bash $demo_root/start.sh logs"
}

restart_all() {
  # A normal restart must not silently switch an existing validation/user DB.
  if [ -z "${PRODUCE_STATE_DB:-}" ]; then
    local previous_db
    previous_db=$(systemctl --user show "$main_unit" -p Environment --value 2>/dev/null | \
      /usr/bin/python3 -c 'import shlex,sys; print(next((v.split("=",1)[1] for v in shlex.split(sys.stdin.read()) if v.startswith("PRODUCE_STATE_DB=")), ""))')
    if [ -n "$previous_db" ]; then export PRODUCE_STATE_DB="$previous_db"; fi
  fi
  stop_all
  start_all
}

case "${1:-start}" in
  start) start_all ;;
  panel) start_one "$panel_unit" "$scripts/start_debug_panel.sh" ;;
  stop) stop_all ;;
  restart) restart_all ;;
  status) systemctl --user status "$main_unit" "$panel_unit" --no-pager || true ;;
  logs) journalctl --user -b -u "$main_unit" -n 40 -f ;;
  *) echo "用法：bash $demo_root/start.sh {start|panel|stop|restart|status|logs}" >&2; exit 2 ;;
esac
