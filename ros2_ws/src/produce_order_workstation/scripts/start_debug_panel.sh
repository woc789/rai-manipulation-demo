#!/usr/bin/env bash
set -eo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/runtime_env.sh"
load_launch_profile
resolve_runtime_paths
setup_ros_environment
# Qt deliberately uses system Python, not the model's Conda environment.
exec ros2 run produce_order_workstation debug_panel
