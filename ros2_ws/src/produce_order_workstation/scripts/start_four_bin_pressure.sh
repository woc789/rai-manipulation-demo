#!/usr/bin/env bash
# Explicit test controller only; no simulator, restart or model requests here.
set -eo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/runtime_env.sh"
load_launch_profile
resolve_runtime_paths
setup_ros_environment
exec /usr/bin/python3 -u "$produce_demo_root/ros2_ws/src/produce_order_workstation/scripts/check_four_bin_cycles_live.py" "$@"
