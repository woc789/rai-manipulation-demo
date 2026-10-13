#!/usr/bin/env bash
# Optional serial startup on memory-constrained hosts. Do not duplicate perception.
set -eo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../../../../.." && pwd)
cd "$project_root"
unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH LD_LIBRARY_PATH PYTHONPATH ROS_DISTRO ALL_PROXY all_proxy
export PATH=/usr/local/bin:/usr/bin:/bin
source /opt/ros/jazzy/setup.bash
source /home/wang/miniconda3/etc/profile.d/conda.sh
conda activate rai-jazzy
source install/setup.bash
source src/examples/rai-manipulation-demo/ros2_ws/install/local_setup.bash
export MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 RAI_GDINO_TILE_CLASSES=''
exec ros2 launch rai_bringup openset.launch.py
