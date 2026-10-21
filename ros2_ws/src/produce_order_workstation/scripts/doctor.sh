#!/usr/bin/env bash
set -eo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/runtime_env.sh"
load_launch_profile
resolve_runtime_paths
setup_ros_environment
setup_model_environment
exec python "$produce_demo_root/scripts/doctor.py"
