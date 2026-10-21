#!/usr/bin/env bash
set -eo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/runtime_env.sh"
load_launch_profile
resolve_runtime_paths
setup_ros_environment
setup_model_environment
cd "$PRODUCE_RAI_ROOT"
exec ros2 launch produce_order_workstation workstation.launch.py "$@"
