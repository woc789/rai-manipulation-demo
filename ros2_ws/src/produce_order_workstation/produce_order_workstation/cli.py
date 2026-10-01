"""Run one structured order against the RAI manipulation scene."""

import argparse
import json
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
import rclpy
from rai.communication.ros2.connectors import ROS2Connector
import yaml

from .order import load_order, plan_placements
from .runtime import Workstation


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
        help="Spawn demo objects in the empty RoboticManipulationBenchmark level",
    )
    parser.add_argument(
        "--scene", type=Path, default=share / "config/demo_scene.yaml"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    order = load_order(args.order, config)
    report_path = args.report or (
        Path.home() / ".local/share/produce_order_workstation/reports" / f"{order.order_id}.json"
    )
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
    connector = ROS2Connector(executor_type="single_threaded")
    workstation = Workstation(connector, config)
    try:
        workstation.event(order.order_id, "STARTING")
        workstation.wait_until_ready(with_scene=args.prepare_scene)
        if args.prepare_scene:
            workstation.event(order.order_id, "PREPARING_SCENE")
            workstation.prepare_scene(args.scene, order)
        result = workstation.run(order)
    except Exception as exc:
        workstation.event(order.order_id, "ORDER_FAILED", reason=str(exc))
        result = {
            "order_id": order.order_id,
            "status": "FAILED",
            "completed": 0,
            "requested": order.total_items,
            "error": str(exc),
            "events": workstation.events,
        }
    finally:
        connector.shutdown()
        rclpy.shutdown()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Order report: {report_path}", flush=True)
    if result["status"] != "COMPLETED":
        raise SystemExit(1)
