#!/usr/bin/env bash
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
export LD_LIBRARY_PATH=/home/wang/.local/share/rai-deps/behaviortree-jazzy/prefix/opt/ros/jazzy/lib:${LD_LIBRARY_PATH:-}
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
tests=src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/test
if [[ ${1:-unit} == integration ]]; then
  export ROS_DOMAIN_ID=71 PRODUCE_ROS_INTEGRATION=1
  exec python -m pytest "$tests/test_bt_ros_integration.py" "$tests/test_mcp_ros_integration.py" "$tests/test_queue_ros_integration.py" -o addopts='' -q --tb=short
fi
exec python -m pytest "$tests" --ignore="$tests/test_operator_panel.py" --ignore="$tests/test_debug_panel.py" -o addopts='' -q --tb=short
