"""Submit a structured order to the persistent manager; never spawn another host."""

import argparse
import json
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.node import Node
from produce_order_interfaces.srv import SubmitOrder
from produce_order_interfaces.msg import OrderLine
import yaml

from .order import load_order, plan_placements


def main() -> None:
    share = Path(get_package_share_directory("produce_order_workstation"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order", type=Path, default=share / "config/demo_order.yaml")
    parser.add_argument(
        "--config", type=Path, default=share / "config/workstation.yaml"
    )
    parser.add_argument(
        "--prepare-scene",
        action="store_true",
        help="Legacy flag; scene preparation is now owned by the persistent order service",
    )
    parser.add_argument(
        "--scene", type=Path, default=share / "config/demo_scene.yaml"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, help="Optional registration receipt, not the execution report")
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    order = load_order(args.order, config)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "order_id": order.order_id,
                    "zone": order.zone,
                    "items": [vars(line) for line in order.lines],
                    "placements": [
                        {
                            "sku": item.sku,
                            "bin_id": f"{order.zone}{item.bin_index + 1}",
                            "offset_m": [item.offset_x, item.offset_y],
                        }
                        for item in plan_placements(order, config)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    rclpy.init()
    node = Node("produce_order_cli")
    try:
        client = node.create_client(SubmitOrder, "/produce_orders/submit")
        if not client.wait_for_service(timeout_sec=5):
            raise RuntimeError("Order service unavailable; start the project before submitting")
        future = client.call_async(SubmitOrder.Request(order_id=order.order_id, zone=order.zone,
            priority=0, lines=[OrderLine(sku=line.sku, quantity=line.quantity) for line in order.lines]))
        rclpy.spin_until_future_complete(node, future, timeout_sec=15)
        if not future.done():
            raise RuntimeError("Registration response timed out; check the queue before retrying")
        response = future.result()
        receipt = {"order_id": order.order_id,
                   "status": "REGISTERED" if response.success else "REJECTED",
                   "message": response.message}
    finally:
        node.destroy_node()
        rclpy.shutdown()
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    print("受理不等于执行完成；请在订单面板查看队列、进度与最终报告。", flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not response.success:
        raise SystemExit(1)
