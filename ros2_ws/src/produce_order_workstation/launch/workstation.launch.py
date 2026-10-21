"""Start an empty O3DE benchmark workcell and the RAI manipulation stack."""

import os
import time
import uuid
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
import yaml

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    ExecuteProcess,
    IncludeLaunchDescription,
    SetEnvironmentVariable,
)
from launch.conditions import IfCondition
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    demo_root = Path(
        os.getenv("PRODUCE_DEMO_ROOT", str(Path(__file__).resolve().parents[4]))
    )
    rai_root = Path(os.getenv("PRODUCE_RAI_ROOT", str(demo_root.parents[2])))
    simulator = os.getenv(
        "PRODUCE_SIMULATOR_BINARY",
        str(
            rai_root
            / "demo_assets/manipulation/RAIManipulationDemo/RAIManipulationDemo.GameLauncher"
        ),
    )
    config = yaml.safe_load(
        (
            Path(get_package_share_directory("produce_order_workstation"))
            / "config/workstation.yaml"
        ).read_text()
    )
    return LaunchDescription(
        [
            SetEnvironmentVariable("PRODUCE_SIMULATION_INSTANCE", uuid.uuid4().hex),
            SetEnvironmentVariable("PRODUCE_SIMULATION_STARTED_AT", str(time.time())),
            # Use the original detector; layout, not repeated tiled inference,
            # is the default way to keep this learning scene usable.
            SetEnvironmentVariable("RAI_GDINO_TILE_CLASSES", ""),
            DeclareLaunchArgument(
                "simulator_prefix",
                default_value="",
                description="Optional debugger prefix; normal startup has none",
            ),
            DeclareLaunchArgument(
                "enable_perception",
                default_value="true",
                description="Load RGB-D detection and segmentation required by Agent orders",
            ),
            ExecuteProcess(
                prefix=LaunchConfiguration("simulator_prefix"),
                cmd=[
                    simulator,
                    "+LoadLevel",
                    "RoboticManipulationBenchmark",
                    "-bg_ConnectToAssetProcessor=0",
                    # Bound render/tick load during long mixed vision/motion runs.
                    # This is a load mitigation, not a fix for a native heap abort.
                    "+sys_MaxFPS",
                    "30",
                ],
                cwd=str(rai_root),
                on_exit=[
                    EmitEvent(
                        event=Shutdown(
                            reason="Simulator exited; stop dependent workcell processes, retain database evidence"
                        )
                    )
                ],
                output="screen",
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    str(
                        demo_root
                        / "Project/Examples/panda_moveit_config_demo.launch.py"
                    )
                )
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    [FindPackageShare("rai_bringup"), "/launch/openset.launch.py"]
                ),
                condition=IfCondition(LaunchConfiguration("enable_perception")),
            ),
            Node(
                package="robotic_manipulation",
                executable="robotic_manipulation",
                output="screen",
                parameters=[
                    {
                        "use_sim_time": True,
                        "post_grasp_lift_z_m": config["motion"]["post_grasp_lift_z_m"],
                    }
                ],
            ),
            Node(
                package="produce_order_workstation",
                executable="agent_server",
                output="screen",
            ),
            Node(
                package="produce_order_bt",
                executable="fulfillment_tree",
                output="screen",
            ),
            Node(
                package="produce_order_workstation",
                executable="order_server",
                output="screen",
            ),
        ]
    )
