"""Persistent ROS 2 Action server for one produce-order workcell."""

import argparse
import json
from pathlib import Path
from threading import Lock

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.utilities import remove_ros_args
from rai.communication.ros2.connectors import ROS2Connector
import yaml

from produce_order_interfaces.action import ExecuteOrder

from .order import Order, OrderCancelled, OrderLine, validate_order
from .runtime import Workstation


def order_from_goal(goal: ExecuteOrder.Goal) -> Order:
    return Order(
        order_id=goal.order_id,
        zone=goal.zone,
        lines=tuple(OrderLine(line.sku, int(line.quantity)) for line in goal.lines),
    )


class OrderActionServer(Node):
    """Accept one order at a time; publish progress and retain a JSON report."""

    def __init__(self, config_path: Path, scene_path: Path):
        super().__init__("produce_order_server")
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.scene_path = scene_path
        self._lock = Lock()
        self._busy = False
        self.connector = ROS2Connector(
            "produce_order_runtime", executor_type="single_threaded"
        )
        self.workstation = Workstation(self.connector, self.config)
        self._server = ActionServer(
            self,
            ExecuteOrder,
            "/produce_orders/execute",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
            callback_group=ReentrantCallbackGroup(),
        )

    @staticmethod
    def report_path(order_id: str) -> Path:
        return (
            Path.home()
            / ".local/share/produce_order_workstation/reports"
            / f"{order_id}.json"
        )

    def goal_callback(self, goal: ExecuteOrder.Goal) -> GoalResponse:
        try:
            validate_order(order_from_goal(goal), self.config)
        except ValueError as exc:
            self.get_logger().warning(f"Rejected invalid order: {exc}")
            return GoalResponse.REJECT
        with self._lock:
            if self._busy:
                self.get_logger().warning("Rejected order: workcell is busy")
                return GoalResponse.REJECT
            if self.report_path(goal.order_id).exists():
                self.get_logger().warning("Rejected order: order ID already has a report")
                return GoalResponse.REJECT
            self._busy = True
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle) -> CancelResponse:
        feedback = ExecuteOrder.Feedback()
        feedback.stage = "CANCEL_REQUESTED"
        feedback.detail_json = json.dumps(
            {"stage": "CANCEL_REQUESTED", "detail": "Waiting for a safe item boundary"}
        )
        goal_handle.publish_feedback(feedback)
        return CancelResponse.ACCEPT

    def _publish_feedback(self, goal_handle, entry: dict) -> None:
        feedback = ExecuteOrder.Feedback()
        feedback.stage = entry["stage"]
        feedback.sku = entry.get("sku", "")
        feedback.completed = int(entry.get("completed", 0))
        feedback.total = int(entry.get("total", 0))
        feedback.detail_json = json.dumps(entry, ensure_ascii=False)
        goal_handle.publish_feedback(feedback)

    def execute_callback(self, goal_handle) -> ExecuteOrder.Result:
        order = order_from_goal(goal_handle.request)
        report_path = self.report_path(order.order_id)
        self.workstation.events = []
        self.workstation.event_sink = lambda entry: self._publish_feedback(
            goal_handle, entry
        )
        self.workstation.cancel_requested = lambda: goal_handle.is_cancel_requested
        try:
            self.workstation.event(order.order_id, "STARTING")
            self.workstation.check_cancel()
            self.workstation.wait_until_ready(
                with_scene=goal_handle.request.prepare_scene
            )
            self.workstation.check_cancel()
            if goal_handle.request.prepare_scene:
                self.workstation.event(order.order_id, "PREPARING_SCENE")
                self.workstation.prepare_scene(self.scene_path, order)
            self.workstation.check_cancel()
            result = self.workstation.run(order)
        except OrderCancelled as exc:
            self.workstation.event(order.order_id, "ORDER_CANCELLED", reason=str(exc))
            result = self._failure_result(order, "CANCELLED", str(exc))
        except Exception as exc:
            self.get_logger().error(f"Order {order.order_id} failed: {exc}")
            self.workstation.event(order.order_id, "ORDER_FAILED", reason=str(exc))
            result = self._failure_result(order, "FAILED", str(exc))
        finally:
            self.workstation.event_sink = None
            self.workstation.cancel_requested = lambda: False

        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response = ExecuteOrder.Result()
            response.status = result["status"]
            response.completed = result["completed"]
            response.requested = result["requested"]
            response.error = result["error"] or ""
            response.report_path = str(report_path)
            if result["status"] == "COMPLETED":
                goal_handle.succeed()
            elif result["status"] == "CANCELLED":
                goal_handle.canceled()
            else:
                goal_handle.abort()
            return response
        finally:
            with self._lock:
                self._busy = False

    def _failure_result(self, order: Order, status: str, error: str) -> dict:
        return {
            "order_id": order.order_id,
            "status": status,
            "completed": 0,
            "requested": order.total_items,
            "error": error,
            "events": self.workstation.events,
        }

    def close(self) -> None:
        self._server.destroy()
        self.connector.shutdown()
        self.destroy_node()


def main() -> None:
    share = Path(get_package_share_directory("produce_order_workstation"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=share / "config/workstation.yaml")
    parser.add_argument("--scene", type=Path, default=share / "config/demo_scene.yaml")
    args = parser.parse_args(remove_ros_args()[1:])
    rclpy.init()
    server = OrderActionServer(args.config, args.scene)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(server)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        server.close()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
