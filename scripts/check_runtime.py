"""Bounded ROS health probe. Only subscribes and checks discovery; never sends goals."""

import argparse
import json
import sys
import time

import rclpy
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rosgraph_msgs.msg import Clock
from std_msgs.msg import String
from produce_order_interfaces.action import (
    ExecuteAgent,
    ExecuteMotion,
    ExecuteOrder,
    RunFulfillment,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=8)
    args = parser.parse_args()
    if not 0 < args.timeout <= 600:
        parser.error("timeout must be in (0,600]")
    rclpy.init()
    node = rclpy.create_node("produce_launch_health")
    status, times, clock_progress = {}, {}, [None, 0]

    def state(message):
        try:
            decoded = json.loads(message.data)
        except ValueError:
            return
        if not isinstance(decoded, dict):
            return
        status.clear()
        status.update(decoded)
        times["status"] = time.monotonic()

    def clock(message):
        value = (message.clock.sec, message.clock.nanosec)
        if value != clock_progress[0]:
            clock_progress[0] = value
            clock_progress[1] += 1
            times["clock"] = time.monotonic()

    subscriptions = [
        node.create_subscription(
            String,
            "/produce_orders/workcell_status",
            state,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        ),
        node.create_subscription(
            Clock,
            "/clock",
            clock,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
        ),
    ]
    actions = {
        "订单 Action": ActionClient(node, ExecuteOrder, "/produce_orders/execute"),
        "Agent Action": ActionClient(node, ExecuteAgent, "/produce_agent/execute"),
        "行为树 Action": ActionClient(
            node, RunFulfillment, "/produce_orders/internal/run_tree"
        ),
        "运动 Action": ActionClient(
            node, ExecuteMotion, "/produce_orders/execute_motion"
        ),
    }
    required_services = {"/produce_orders/submit", "/detection", "/segmentation"}
    deadline = time.monotonic() + args.timeout
    previous = None
    fresh, missing_actions, missing_services = False, list(actions), required_services
    try:
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.2)
            now = time.monotonic()
            current = status.get("state", "等待状态消息")
            if current != previous:
                print("[状态]", current, status.get("reason", ""), flush=True)
                previous = current
            fresh = (
                all(now - times.get(kind, 0) < 3 for kind in ("status", "clock"))
                and clock_progress[1] >= 2
            )
            missing_actions = [
                name for name, client in actions.items() if not client.server_is_ready()
            ]
            missing_services = required_services - {
                name for name, _ in node.get_service_names_and_types()
            }
            if not fresh or missing_actions or missing_services:
                continue
            if current in {"READY", "RUNNING"}:
                print(
                    "[就绪] 时钟、状态、订单/Agent/行为树/运动 Action 和视觉服务在线。"
                )
                print("模型 Key/额度未实际调用验证；就绪不等于订单保证成功。")
                return 0
            if current in {
                "RECOVERY_REQUIRED",
                "INVENTORY_ERROR",
                "SCENE_OFFLINE",
                "DATABASE_ERROR",
            }:
                print(
                    "[需处理] 服务已上线，但业务未就绪。按 README 的状态处理表操作，不要重复下单。"
                )
                return 2
        print("[未就绪] 达到等待上限；后台不因此被强制停止。")
        print(
            "时钟/状态有效：",
            fresh,
            "；缺失 Action：",
            missing_actions,
            "；缺失服务：",
            sorted(missing_services),
        )
        print("运行 bash start.sh logs-once；只需重开界面时运行 bash start.sh panel。")
        return 1
    finally:
        for client in actions.values():
            client.destroy()
        for subscription in subscriptions:
            node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
