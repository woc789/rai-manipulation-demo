from glob import glob
from setuptools import find_packages, setup


package_name = "produce_order_workstation"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", glob("config/*.yaml")),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools", "PyYAML", "openai>=1.0", "mcp==1.30.0"],
    scripts=["scripts/run_order", "scripts/order_server", "scripts/debug_panel"],
    zip_safe=True,
    maintainer="wang",
    maintainer_email="wang@example.com",
    description="Structured produce order picking on the RAI O3DE demo",
    license="Apache-2.0",
)
