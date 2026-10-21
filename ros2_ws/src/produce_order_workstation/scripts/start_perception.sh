#!/usr/bin/env bash
# Developer-only perception startup. Never duplicate the normal full launcher.
set -eo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/runtime_env.sh"
load_launch_profile
resolve_runtime_paths
setup_ros_environment
setup_model_environment
cd "$PRODUCE_RAI_ROOT"
export RAI_GDINO_TILE_CLASSES=''
exec ros2 launch rai_bringup openset.launch.py
