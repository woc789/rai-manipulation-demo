#!/usr/bin/env bash
set -eo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/runtime_env.sh"
load_launch_profile
resolve_runtime_paths
setup_ros_environment
exec /usr/bin/python3 "$produce_demo_root/scripts/check_runtime.py" "$@"
