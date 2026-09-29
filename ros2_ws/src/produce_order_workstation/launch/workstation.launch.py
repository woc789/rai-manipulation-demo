"""Start an empty O3DE benchmark workcell and the RAI manipulation stack."""

from launch import LaunchDescription
from launch.actions import ExecuteProcess, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    return LaunchDescription(
        [
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
                )
            ),
            Node(
                package="robotic_manipulation",
                executable="robotic_manipulation",
                output="screen",
                parameters=[{"use_sim_time": True}],
            ),
        ]
    )
