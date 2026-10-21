"""Read-only preflight: no model API requests, model loading or ROS orders."""

import importlib
import os
from pathlib import Path
import subprocess
import sys


def main():
    failures = []

    def check(label, okay, hint):
        print(f"[{'OK' if okay else 'FAIL'}] {label}", flush=True)
        if not okay:
            failures.append(label)
            print(f"  处理：{hint}", flush=True)

    print("本地依赖检查；不会检测模型额度，也不代表视觉识别通过。")
    for key in ("PRODUCE_RAI_ROOT", "PRODUCE_DEMO_ROOT", "PRODUCE_STATE_DB"):
        print(key, os.environ[key])
    check("ROS Jazzy", os.getenv("ROS_DISTRO") == "jazzy", "不要混用 Kilted 与 Jazzy。")
    binary = Path(os.environ["PRODUCE_SIMULATOR_BINARY"])
    check(
        "O3DE 可执行文件",
        binary.is_file() and os.access(binary, os.X_OK),
        "设置 PRODUCE_SIMULATOR_BINARY，见 docs/INSTALL.md。",
    )
    check(
        "O3DE Cache",
        (binary.parent / "Cache").is_dir(),
        "需要完整仿真发布包，不是只复制一个可执行文件。",
    )
    for module in (
        "rclpy",
        "mcp",
        "openai",
        "httpx",
        "rai",
        "rai_sim",
        "rai_perception",
        "produce_order_interfaces",
    ):
        try:
            importlib.import_module(module)
            okay = True
        except Exception as exc:
            okay = False
            print("  导入异常类型：", type(exc).__name__)
        check(module, okay, "按 docs/INSTALL.md 安装依赖，并构建两个 ROS overlay。")
    from ament_index_python.packages import get_package_prefix

    binaries = {
        "produce_order_bt": "fulfillment_tree",
        "robotic_manipulation": "robotic_manipulation",
    }
    for package in (
        *binaries,
        "produce_order_workstation",
        "rai_bringup",
        "moveit_ros_move_group",
    ):
        try:
            prefix = Path(get_package_prefix(package))
            executable = binaries.get(package)
            okay = not executable or (prefix / "lib" / package / executable).is_file()
        except LookupError:
            okay = False
        check(package, okay, "构建缺失的 ROS 包；重启不能补全依赖。")
    weights = Path.home() / ".cache/rai/vision/weights"
    for filename in ("groundingdino_swint_ogc.pth", "sam2_hiera_large.pt"):
        path = weights / filename
        check(
            filename,
            path.is_file() and path.stat().st_size > 1_000_000,
            "按 docs/INSTALL.md 准备权重；本机已有，不需重复下载。",
        )
    try:
        from transformers import AutoTokenizer

        AutoTokenizer.from_pretrained("bert-base-uncased", local_files_only=True)
        okay = True
    except Exception:
        okay = False
    check(
        "BERT tokenizer 离线缓存",
        okay,
        "先在线准备 bert-base-uncased 缓存，再使用离线启动。",
    )
    from huggingface_hub import try_to_load_from_cache

    cached = try_to_load_from_cache("bert-base-uncased", "model.safetensors")
    check(
        "BERT safetensors 缓存",
        isinstance(cached, str) and Path(cached).is_file(),
        "在线准备 bert-base-uncased/model.safetensors，见 docs/INSTALL.md。",
    )
    patch = Path(os.environ["PRODUCE_DEMO_ROOT"]) / "patches/rai-workcell.patch"
    result = subprocess.run(
        [
            "git",
            "-C",
            os.environ["PRODUCE_RAI_ROOT"],
            "apply",
            "--reverse",
            "--check",
            str(patch),
        ],
        capture_output=True,
        timeout=15,
    )
    check(
        "RAI 工作站兼容补丁",
        result.returncode == 0,
        "按 patches/README.md 检查固定基线和补丁，勿对已有目录直接 reset。",
    )
    from produce_order_workstation.agent import _key_from_private_file

    # systemd launcher intentionally does not put the key in its visible environment.
    check(
        "私有模型 Key 文件",
        bool(_key_from_private_file()),
        "配置 ~/.config/rai/bailian.env 并 chmod 600；不要把 Key 放进 Git。",
    )
    qt = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            "from PyQt5.QtWidgets import QApplication; QApplication(['doctor','-platform','offscreen'])",
        ],
        capture_output=True,
        timeout=15,
    )
    check(
        "系统 Python Qt",
        qt.returncode == 0,
        "安装 python3-pyqt5；Qt 不使用 Conda Python。",
    )
    check("DISPLAY", bool(os.getenv("DISPLAY")), "请在已登录图形桌面的终端执行。")
    for package, executable in binaries.items():
        try:
            path = Path(get_package_prefix(package)) / "lib" / package / executable
            result = subprocess.run(
                ["ldd", str(path)], capture_output=True, text=True, timeout=10
            )
            okay = result.returncode == 0 and "not found" not in result.stdout
        except (LookupError, OSError):
            okay = False
        check(f"{package} 动态库", okay, "检查 PRODUCE_BT_PREFIX 和 Jazzy 动态库。")
    print(
        "检查失败，请按提示处理。"
        if failures
        else "依赖检查通过。下一步启动并等待业务就绪；不保证模型账户额度可用。"
    )
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
