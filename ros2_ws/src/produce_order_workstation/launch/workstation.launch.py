"""Start an empty O3DE benchmark workcell and the RAI manipulation stack."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "enable_perception",
                default_value="true",
                description="Load RGB-D detection and segmentation required by Agent orders",
            ),
            ExecuteProcess(
                cmd=[
                    "demo_assets/manipulation/RAIManipulationDemo/"
                    "RAIManipulationDemo.GameLauncher",
                    "+LoadLevel",
                    "RoboticManipulationBenchmark",
                    "-bg_ConnectToAssetProcessor=0",
                ],
                output="screen",
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    "src/examples/rai-manipulation-demo/Project/Examples/"
                    "panda_moveit_config_demo.launch.py"
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
                parameters=[{"use_sim_time": True}],
            ),
            Node(
                package="produce_order_workstation",
                executable="order_server",
                output="screen",
            ),
        ]
    )
