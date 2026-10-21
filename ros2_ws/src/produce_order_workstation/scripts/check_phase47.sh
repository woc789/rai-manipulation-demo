#!/usr/bin/env bash
set -eo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/runtime_env.sh"
load_launch_profile
resolve_runtime_paths
setup_ros_environment
setup_model_environment
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
tests="$produce_demo_root/ros2_ws/src/produce_order_workstation/test"
cd "$PRODUCE_RAI_ROOT"
if [[ ${1:-unit} == integration ]]; then
  export ROS_DOMAIN_ID=71 PRODUCE_ROS_INTEGRATION=1
  exec python -m pytest "$tests/test_bt_ros_integration.py" "$tests/test_mcp_ros_integration.py" "$tests/test_queue_ros_integration.py" -o addopts='' -q --tb=short
fi
export ROS_DOMAIN_ID=74
exec python -m pytest "$tests" --ignore="$tests/test_operator_panel.py" --ignore="$tests/test_debug_panel.py" -o addopts='' -q --tb=short
