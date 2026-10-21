#!/usr/bin/env bash
# One launcher for the simulator and native Qt panel, from any working directory.
set -eo pipefail
demo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
scripts="$demo_root/ros2_ws/src/produce_order_workstation/scripts"
main_unit=produce-order-workstation.service
panel_unit=produce-order-panel.service
case "${1:-start}" in
  stop|logs|logs-once) ;; # Stopping/reading logs must also work with broken config.
  *) source "$scripts/runtime_env.sh"; load_launch_profile ;;
esac

prepare_display() {
  if [ -z "${DISPLAY:-}" ]; then
    for number in 0 1; do
      if [ -S "/tmp/.X11-unix/X$number" ]; then export DISPLAY=":$number"; break; fi
    done
  fi
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [ -z "${XAUTHORITY:-}" ] && [ -f "$XDG_RUNTIME_DIR/gdm/Xauthority" ]; then
    export XAUTHORITY="$XDG_RUNTIME_DIR/gdm/Xauthority"
  fi
  if [ -z "${DISPLAY:-}" ]; then echo '没有图形桌面 DISPLAY；请在已登录桌面的终端运行。' >&2; return 1; fi
}

unit_setting() {
  systemctl --user show "$main_unit" -p Environment --value 2>/dev/null | \
    /usr/bin/python3 -c 'import shlex,sys; key=sys.argv[1]+"="; print(next((v[len(key):] for v in shlex.split(sys.stdin.read()) if v.startswith(key)), ""))' "$1"
}

check_running_configuration() {
  local name previous desired
  if ! systemctl --user is-active --quiet "$main_unit"; then return; fi
  for name in PRODUCE_STATE_DB PRODUCE_ROS_DOMAIN_ID PRODUCE_AGENT_MODEL PRODUCE_AGENT_PROXY_URL PRODUCE_AGENT_BASE_URL; do
    if [[ ! -v "$name" ]]; then continue; fi
    previous=$(unit_setting "$name")
    desired=${!name}
    if [ "$previous" != "$desired" ] && { [ -n "$previous" ] || [ -n "$desired" ]; }; then
      echo "运行中服务的 $name 与当前配置不同；start 不会切换运行中的配置，请处理现场后使用 restart。" >&2
      return 1
    fi
  done
}

start_one() {
  local unit=$1 script=$2
  if systemctl --user is-active --quiet "$unit"; then
    echo "$unit 已在运行，不重复启动"
    return
  fi
  prepare_display
  systemctl --user reset-failed "$unit" 2>/dev/null || true
  local state_args=()
  local name
  for name in PRODUCE_LAUNCH_CONFIG PRODUCE_RAI_ROOT PRODUCE_ROS_SETUP PRODUCE_CONDA_SH PRODUCE_CONDA_ENV PRODUCE_BT_PREFIX PRODUCE_SIMULATOR_BINARY PRODUCE_STATE_DB PRODUCE_ROS_DOMAIN_ID PRODUCE_AGENT_MODEL PRODUCE_AGENT_BASE_URL PRODUCE_AGENT_PROXY_URL; do
    if [[ -v "$name" ]]; then state_args+=(--setenv="$name=${!name}"); fi
  done
  # Never put DASHSCOPE_API_KEY into systemd's inspectable Environment field.
  systemd-run --user --collect --unit="$unit" \
    --setenv="DISPLAY=$DISPLAY" \
    --setenv="XAUTHORITY=${XAUTHORITY:-}" \
    --setenv="XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" \
    "${state_args[@]}" \
    --property="WorkingDirectory=$demo_root" \
    --property=TimeoutStopSec=25 \
    /bin/bash "$script"
}

stop_all() {
  for unit in produce-four-bin-pressure.service produce-large-order-validation.service produce-large-order-panel.service "$panel_unit" "$main_unit"; do
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
  check_running_configuration
  if ! systemctl --user is-active --quiet "$main_unit" && \
     pgrep -u "$(id -u)" -f 'RAIManipulationDemo[.]GameLauncher' >/dev/null; then
    echo "检测到另一套仿真进程；请先在其启动终端按 Ctrl+C，避免两套仿真争用内存。" >&2
    exit 1
  fi
  prepare_display
  if ! systemctl --user is-active --quiet "$main_unit"; then
    bash "$scripts/doctor.sh" || { echo '预检查失败，未启动重型仿真。' >&2; return 1; }
  fi
  start_one "$main_unit" "$scripts/start_workstation.sh"
  start_one "$panel_unit" "$scripts/start_debug_panel.sh"
  echo "后台启动已请求；正在等待实际 ROS 就绪（不是只检查 systemd active）。"
  bash "$scripts/check_runtime.sh" --timeout "${PRODUCE_READY_TIMEOUT_S:-240}"
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
  doctor) prepare_display; bash "$scripts/doctor.sh" ;;
  ready) bash "$scripts/check_runtime.sh" --timeout 8 ;;
  status)
    systemctl --user show "$main_unit" "$panel_unit" -p Id -p ActiveState -p SubState --no-pager
    bash "$scripts/check_runtime.sh" --timeout 8 ;;
  logs) journalctl --user -b -u "$main_unit" -u "$panel_unit" -n 40 -f ;;
  logs-once) journalctl --user -b -u "$main_unit" -u "$panel_unit" -n 80 --no-pager ;;
  *) echo "用法：bash $demo_root/start.sh {doctor|start|panel|stop|restart|status|ready|logs|logs-once}" >&2; exit 2 ;;
esac
