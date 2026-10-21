#!/usr/bin/env bash
# Shared resolution; sourcing does not start a service or robot.
produce_demo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
load_launch_profile() {
  local profile_file name index
  local -a saved_names=() saved_values=()
  local -a profile_names=(PRODUCE_RAI_ROOT PRODUCE_ROS_SETUP PRODUCE_CONDA_SH PRODUCE_CONDA_ENV PRODUCE_BT_PREFIX PRODUCE_SIMULATOR_BINARY PRODUCE_STATE_DB PRODUCE_ROS_DOMAIN_ID PRODUCE_AGENT_MODEL PRODUCE_AGENT_BASE_URL PRODUCE_AGENT_PROXY_URL PRODUCE_READY_TIMEOUT_S)
  profile_file=${PRODUCE_LAUNCH_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/produce_order_workstation/launch.env}
  for name in "${profile_names[@]}"; do
    if [[ -v "$name" ]]; then saved_names+=("$name"); saved_values+=("${!name}"); fi
  done
  if [ -f "$profile_file" ]; then
    # Trusted user-owned Bash profile, never a downloaded untrusted env file.
    source "$profile_file"
  elif [ -n "${PRODUCE_LAUNCH_CONFIG:-}" ]; then
    echo "启动配置不存在：$profile_file" >&2; return 1
  fi
  for index in "${!saved_names[@]}"; do printf -v "${saved_names[$index]}" '%s' "${saved_values[$index]}"; done
  for name in "${profile_names[@]}"; do if [[ -v "$name" ]]; then export "$name"; fi; done
  if [ -f "$profile_file" ]; then export PRODUCE_LAUNCH_CONFIG="$profile_file"; fi
}
resolve_runtime_paths() {
  local candidate file
  candidate=$(cd -- "$produce_demo_root/../../.." && pwd)
  if [ -z "${PRODUCE_RAI_ROOT:-}" ] && [ -d "$candidate/src/rai_core" ]; then PRODUCE_RAI_ROOT=$candidate; fi
  if [ ! -d "${PRODUCE_RAI_ROOT:-}/src/rai_core" ]; then
    echo '未找到 RAI 源码：请按 config/launch.env.example 设置 PRODUCE_RAI_ROOT。' >&2; return 1
  fi
  export PRODUCE_RAI_ROOT PRODUCE_DEMO_ROOT="$produce_demo_root"
  export PRODUCE_ROS_SETUP=${PRODUCE_ROS_SETUP:-/opt/ros/jazzy/setup.bash}
  export PRODUCE_CONDA_SH=${PRODUCE_CONDA_SH:-$HOME/miniconda3/etc/profile.d/conda.sh}
  export PRODUCE_CONDA_ENV=${PRODUCE_CONDA_ENV:-rai-jazzy}
  export PRODUCE_BT_PREFIX=${PRODUCE_BT_PREFIX-$HOME/.local/share/rai-deps/behaviortree-jazzy/prefix/opt/ros/jazzy}
  export PRODUCE_SIMULATOR_BINARY=${PRODUCE_SIMULATOR_BINARY:-$PRODUCE_RAI_ROOT/demo_assets/manipulation/RAIManipulationDemo/RAIManipulationDemo.GameLauncher}
  export PRODUCE_STATE_DB=${PRODUCE_STATE_DB:-$HOME/.local/share/produce_order_workstation/workcell-mcp.sqlite3}
  export PRODUCE_ROS_DOMAIN_ID=${PRODUCE_ROS_DOMAIN_ID:-0}
  for file in "$PRODUCE_ROS_SETUP" "$PRODUCE_RAI_ROOT/install/local_setup.bash" "$produce_demo_root/ros2_ws/install/local_setup.bash"; do
    if [ ! -r "$file" ]; then echo "缺少启动依赖：$file；先按 docs/INSTALL.md 完成构建。" >&2; return 1; fi
  done
}
setup_ros_environment() {
  unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH LD_LIBRARY_PATH PYTHONPATH ROS_DISTRO ALL_PROXY all_proxy
  export PATH=/usr/local/bin:/usr/bin:/bin
  source "$PRODUCE_ROS_SETUP"
  if [ -n "$PRODUCE_BT_PREFIX" ] && [ -d "$PRODUCE_BT_PREFIX/lib" ]; then
    export LD_LIBRARY_PATH="$PRODUCE_BT_PREFIX/lib:${LD_LIBRARY_PATH:-}"
    export CMAKE_PREFIX_PATH="$PRODUCE_BT_PREFIX:${CMAKE_PREFIX_PATH:-}"
  fi
  source "$PRODUCE_RAI_ROOT/install/local_setup.bash"
  source "$produce_demo_root/ros2_ws/install/local_setup.bash"
  export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST ROS_DOMAIN_ID="$PRODUCE_ROS_DOMAIN_ID"
}
setup_model_environment() {
  if [ ! -r "$PRODUCE_CONDA_SH" ]; then echo "Conda 初始化脚本不存在：$PRODUCE_CONDA_SH" >&2; return 1; fi
  source "$PRODUCE_CONDA_SH"
  conda activate "$PRODUCE_CONDA_ENV"
  export MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
}
