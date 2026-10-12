#!/usr/bin/env bash
set -eo pipefail

workstation_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../../../../.." && pwd)
cd "$workstation_root"

# The desktop session exports Kilted paths. Mixing them with Jazzy makes
# MoveIt/O3DE fail at dynamic linking, so build this process environment anew.
unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH LD_LIBRARY_PATH PYTHONPATH ROS_DISTRO
export PATH=/usr/local/bin:/usr/bin:/bin
source /opt/ros/jazzy/setup.bash
source /home/wang/miniconda3/etc/profile.d/conda.sh
conda activate rai-jazzy
source install/setup.bash
source src/examples/rai-manipulation-demo/ros2_ws/install/local_setup.bash
set -u

# Keep C/C++ and BLAS worker arenas bounded while O3DE and the two vision
# models share a 16 GiB workstation. The API key remains in the private file.
export MALLOC_ARENA_MAX=2
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MKL_NUM_THREADS=2
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
unset ALL_PROXY all_proxy

# Fail before spawning the heavy simulator if the tool gateway dependency is absent.
python -c 'from mcp import ClientSession; from mcp.server.fastmcp import FastMCP' || {
  echo '缺少 MCP SDK：请在 rai-jazzy 环境运行 python -m pip install mcp==1.30.0' >&2
  exit 1
}

exec ros2 launch produce_order_workstation workstation.launch.py "$@"
