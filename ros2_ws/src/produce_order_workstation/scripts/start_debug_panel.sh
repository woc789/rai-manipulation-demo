#!/usr/bin/env bash
set -eo pipefail

workstation_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../../../../.." && pwd)
cd "$workstation_root"
unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH LD_LIBRARY_PATH PYTHONPATH ROS_DISTRO
export PATH=/usr/local/bin:/usr/bin:/bin
source /opt/ros/jazzy/setup.bash
source src/examples/rai-manipulation-demo/ros2_ws/install/local_setup.bash
set -u
exec ros2 run produce_order_workstation debug_panel
