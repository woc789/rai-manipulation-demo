"""Persistent ROS 2 Action server for one produce-order workcell."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock, Thread

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.utilities import remove_ros_args
from rai.communication.ros2.connectors import ROS2Connector
import yaml
from std_msgs.msg import String
from std_srvs.srv import Trigger

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.srv import ConfirmPickup
from produce_order_interfaces.msg import InventoryItem, InventorySnapshot
from geometry_msgs.msg import Point

from .fulfillment import BinManager
from .order import Order, OrderCancelled, OrderLine, validate_order
from .runtime import Workstation


def order_from_goal(goal: ExecuteOrder.Goal) -> Order:
    return Order(
        order_id=goal.order_id,
        zone=goal.zone,
        lines=tuple(OrderLine(line.sku, int(line.quantity)) for line in goal.lines),
    )


def state_after_order(status: str, motion_attempted: bool) -> tuple[str, str, bool]:
    """Only an untouched scene can immediately accept a new order."""
    if status == "COMPLETED":
        return "READY", "上一单已配货完成，箱位保留待取货；桌面库存未自动补满", True
    if motion_attempted:
        return (
            "RECOVERY_REQUIRED",
            "订单中尝试过机械臂动作，现场状态可能改变；请点击“恢复工作台”重置场景",
            False,
        )
    return "READY", "订单在机械臂动作前结束，库存未改变，可以重新提交", True


class OrderActionServer(Node):
    """Accept one order at a time; publish progress and retain a JSON report."""

    def __init__(self, config_path: Path, scene_path: Path):
        super().__init__("produce_order_server")
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.scene_path = scene_path
        self.bin_manager = BinManager(self.config)
        self._lock = Lock()
        self._busy = False
        self._scene_ready = False
        self._state = "INITIALIZING"
        self._state_reason = "正在启动感知服务并准备桌面库存"
        self._status_publisher = self.create_publisher(
            String, "/produce_orders/workcell_status",
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        )
        self._inventory_publisher = self.create_publisher(
            InventorySnapshot, "/produce_orders/inventory",
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self._recovery_service = self.create_service(
            Trigger, "/produce_orders/recover", self.recover_callback,
            callback_group=ReentrantCallbackGroup(),
        )
        self._pickup_service = self.create_service(
            ConfirmPickup, "/produce_orders/confirm_pickup", self.pickup_callback,
            callback_group=ReentrantCallbackGroup(),
        )
        self._status_timer = self.create_timer(1.0, self.publish_status)
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
        Thread(target=self._prepare_initial_stock, daemon=True).start()

    def publish_status(self) -> None:
        with self._lock:
            status = {
                "state": self._state,
                "reason": self._state_reason,
                "can_submit": self._scene_ready and not self._busy and self.bin_manager.can_allocate("A"),
                "can_recover": not self._busy and self._state not in (
                    "INITIALIZING", "REPLENISHING", "RECOVERING"
                ) and self.bin_manager.can_reset(),
                "can_pickup": not self._busy,
                "operation_mode": self.config.get("operation_mode", "classification"),
                "bins": self.bin_manager.snapshot(),
            }
        self._status_publisher.publish(String(data=json.dumps(status, ensure_ascii=False)))
        self.publish_inventory()

    def publish_inventory(self):
        if not hasattr(self, "workstation"):
            return
        snapshot = self.workstation.inventory.snapshot()
        message = InventorySnapshot()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = self.config["camera"]["target_frame"]
        message.epoch = snapshot["epoch"]
        message.scene_ready = self.workstation._scene_prepared and self._state not in (
            "INITIALIZING", "RECOVERING", "REPLENISHING")
        message.persistence = snapshot["persistence"]
        for row in snapshot["items"]:
            item = InventoryItem()
            for key in ("item_id", "sku", "frame_id", "observed_unix_sec", "age_sec", "stale",
                        "classification_verified", "state", "order_id", "observation_source"):
                setattr(item, key, row[key])
            item.position = Point(x=row["position"][0], y=row["position"][1], z=row["position"][2])
            message.items.append(item)
        self._inventory_publisher.publish(message)

    def _set_state(self, state: str, reason: str, *, ready: bool = False) -> None:
        with self._lock:
            self._state = state
            self._state_reason = reason
            self._scene_ready = ready
        self.publish_status()

    def _prepare_initial_stock(self) -> None:
        """Make inventory visible before a customer submits an order."""
        try:
            self.workstation.wait_until_ready(with_scene=True)
            self.workstation.prepare_scene(self.scene_path)
        except Exception as exc:
            self.get_logger().error(f"Could not preload mixed inventory: {exc}")
            self._set_state("RECOVERY_REQUIRED", f"库存初始化失败：{exc}")
            return
        self._set_state("READY", "混合库存已就绪，可以提交订单", ready=True)
        self.get_logger().info("Mixed table inventory is ready: four of each SKU")

    def pickup_callback(self, request, response):
        with self._lock:
            if self._busy or self._state in ("INITIALIZING", "RECOVERING"):
                response.success, response.message = False, "工作台忙，请等待当前操作结束"
                return response
            try:
                allocation = self.bin_manager.pickup(request.order_id)
            except ValueError as exc:
                response.success, response.message = False, str(exc)
                return response
            self._busy = True
        self.publish_status()
        try:
            self.workstation.collect_order(allocation.order, allocation.entities)
            # Keep execution reports immutable; record delivery separately.
            receipt_path = self.report_path(request.order_id).with_suffix(".pickup.json")
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps({
                "order_id": request.order_id, "delivery_state": "COLLECTED",
                "bin_ids": [f"{allocation.order.zone}{i + 1}" for i in allocation.bin_indices],
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self._lock:
                self.bin_manager.confirm_pickup(request.order_id)
            response.success = True
            response.message = f"订单 {request.order_id} 已取货，箱位释放；桌面库存保持不变"
        except Exception as exc:
            # Never free ownership on a partial or failed simulator cleanup.
            response.success, response.message = False, f"取货清理失败，箱位仍保留，可检查后重试：{exc}"
            self.get_logger().error(response.message)
        finally:
            with self._lock:
                self._busy = False
            self.publish_status()
        return response

    def recover_callback(self, _request, response):
        with self._lock:
            if self._busy or self._state in ("INITIALIZING", "REPLENISHING", "RECOVERING"):
                response.success = False
                response.message = "工作台正在执行或准备中，暂不能重置场景"
                return response
            if not self.bin_manager.can_reset():
                response.success = False
                response.message = "请先确认取走所有已完成订单；重置不能清空待取货订单"
                return response
            self._busy = True
            self._scene_ready = False
            self._state = "RECOVERING"
            self._state_reason = "正在清理并重新生成库存与分箱"
        self.publish_status()
        Thread(target=self._recover_scene, daemon=True).start()
        response.success = True
        response.message = "已开始重置场景；等待状态变为 READY 后再提交订单"
        return response

    def _recover_scene(self) -> None:
        try:
            self.workstation.wait_until_ready(with_scene=True)
            self.workstation.prepare_scene(self.scene_path)
        except Exception as exc:
            self.get_logger().error(f"Workcell recovery failed: {exc}")
            self._set_state("RECOVERY_REQUIRED", f"场景恢复失败：{exc}")
        else:
            with self._lock:
                self.bin_manager.reset()
            self._set_state("READY", "场景已重置，库存已就绪", ready=True)
        finally:
            with self._lock:
                self._busy = False
            self.publish_status()

    @staticmethod
    def report_path(order_id: str) -> Path:
        return (
            Path.home()
            / ".local/share/produce_order_workstation/reports"
            / f"{order_id}.json"
        )

    def goal_callback(self, goal: ExecuteOrder.Goal) -> GoalResponse:
        try:
            if not goal.prepare_scene:
                raise ValueError("Agent orders require prepare_scene=true")
            validate_order(order_from_goal(goal), self.config)
        except ValueError as exc:
            self.get_logger().warning(f"Rejected invalid order: {exc}")
            return GoalResponse.REJECT
        with self._lock:
            if self._busy:
                self.get_logger().warning("Rejected order: workcell is busy")
                return GoalResponse.REJECT
            if not self._scene_ready:
                self.get_logger().warning("Rejected order: mixed inventory is not ready")
                return GoalResponse.REJECT
            if self.report_path(goal.order_id).exists():
                self.get_logger().warning("Rejected order: order ID already has a report")
                return GoalResponse.REJECT
            try:
                allocation = self.bin_manager.allocate(order_from_goal(goal))
            except ValueError as exc:
                self.get_logger().warning(f"Rejected order: {exc}")
                return GoalResponse.REJECT
            self._busy = True
            self._scene_ready = False
            self._state = "RUNNING"
            bin_ids = "/".join(f"{goal.zone}{i + 1}" for i in allocation.bin_indices)
            self._state_reason = f"正在执行订单 {goal.order_id}，箱位 {bin_ids}"
        self.publish_status()
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle) -> CancelResponse:
        self._publish_feedback(goal_handle, {
            "stage": "CANCEL_REQUESTED", "detail": "Waiting for a safe item boundary"})
        return CancelResponse.ACCEPT

    def _publish_feedback(self, goal_handle, entry: dict) -> None:
        feedback = ExecuteOrder.Feedback()
        feedback.stage = entry["stage"]
        feedback.sku = entry.get("sku", "")
        feedback.completed = int(entry.get("completed", sum(self.workstation._completed_by_sku.values())))
        feedback.total = int(entry.get("total", sum(line.quantity for line in goal_handle.request.lines)))
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
            allocation = self.bin_manager.orders[order.order_id]
            self.workstation.begin_order(order, bin_index=allocation.bin_indices[0])
            self.workstation.ensure_order_bins(order, allocation.bin_indices)
            self.workstation.event(order.order_id, "STARTING")
            self.workstation.check_cancel()
            self.workstation.event(order.order_id, "USING_PRELOADED_SCENE")
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
            allocation = self.bin_manager.orders[order.order_id]
            scene_uncertain = False
            if result["status"] != "COMPLETED" and not self.workstation.motion_attempted:
                try:
                    self.workstation.remove_order_bins(order, allocation.bin_indices)
                except Exception as exc:
                    scene_uncertain = True
                    self.get_logger().error(f"Failed to remove unused order bin: {exc}")
            result["operation_mode"] = self.config.get("operation_mode", "classification")
            result["bin_ids"] = [f"{order.zone}{i + 1}" for i in allocation.bin_indices]
            result["delivery_state"] = "WAITING_PICKUP" if result["status"] == "COMPLETED" else result["status"]
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
            state, reason, ready = state_after_order(
                result["status"], self.workstation.motion_attempted or scene_uncertain
            )
            with self._lock:
                self.bin_manager.finish(order.order_id, result["status"],
                                        self.workstation.motion_attempted or scene_uncertain,
                                        self.workstation._placed_entities)
                self._state, self._state_reason, self._scene_ready = state, reason, ready
                self._busy = False
            self.publish_status()

    def _failure_result(self, order: Order, status: str, error: str) -> dict:
        return {
            "order_id": order.order_id,
            "status": status,
            "completed": len(self.workstation._placed_entities),
            "requested": order.total_items,
            "lines": [{"sku": line.sku, "quantity": line.quantity} for line in order.lines],
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
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown()
        server.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
