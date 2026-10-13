"""Launch-started Agent runtime; each admitted ROS Action gets isolated decisions."""

import argparse
import json
from pathlib import Path
from threading import Event, Lock
from urllib.parse import urlparse

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.utilities import remove_ros_args
from std_msgs.msg import String
import yaml

from produce_order_interfaces.action import ExecuteAgent, ExecuteBusiness
from .agent import ProduceAgent, create_model
from .mcp_client import WorkstationMCPClient
from .order import Order, OrderCancelled, OrderError, OrderLine, OrderPaused, validate_order


class ProduceAgentService(Node):
    def __init__(self, config, *, model=None):
        super().__init__("produce_agent_service")
        self.config = config
        self._lock = Lock()
        self._busy = False
        self._stopping = Event()
        self._active_order = ""
        self._model_error = ""
        try:
            # Only construct the API client; no model request at startup.
            self.model = model if model is not None else create_model(config["agent"])
        except Exception as exc:
            self.model = None
            self._model_error = str(exc)
        self._status = self.create_publisher(String, "/produce_agent/status",
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self._server = ActionServer(self, ExecuteAgent, "/produce_agent/execute",
            execute_callback=self.execute, goal_callback=self.accept,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup())
        self._timer = self.create_timer(1., self.publish_status)
        self._business_busy = False
        self._business_server = ActionServer(self, ExecuteBusiness, "/produce_agent/business",
            execute_callback=self.execute_business, goal_callback=self.accept_business,
            cancel_callback=lambda _: CancelResponse.ACCEPT, callback_group=ReentrantCallbackGroup())
        self.publish_status()
        self.get_logger().info("Agent runtime online; waiting for admitted tasks (no model request at startup)")

    def publish_status(self):
        self._status.publish(String(data=json.dumps({
            "state": "MODEL_UNAVAILABLE" if self._model_error else "BUSY" if self._busy else "IDLE",
            "order_id": self._active_order, "error": self._model_error,
        }, ensure_ascii=False)))

    def accept(self, request):
        try:
            validate_order(Order(request.order_id, request.zone,
                tuple(OrderLine(line.sku, line.quantity) for line in request.lines)), self.config)
            if getattr(request, "mode", "") not in ("", "run", "select"):
                raise ValueError("Unknown Agent task mode")
            url = urlparse(request.mcp_url)
            if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path != "/mcp" or url.username or url.password or url.query or url.fragment or not request.mcp_token:
                raise ValueError("Invalid local MCP endpoint")
        except (ValueError, OrderError):
            return GoalResponse.REJECT
        with self._lock:
            if self._busy or self._stopping.is_set() or self.model is None:
                return GoalResponse.REJECT
            self._busy = True
            self._active_order = request.order_id
        return GoalResponse.ACCEPT

    def execute(self, handle):
        request = handle.request
        order = Order(request.order_id, request.zone,
            tuple(OrderLine(line.sku, line.quantity) for line in request.lines))

        def cancelled():
            return handle.is_cancel_requested or self._stopping.is_set()

        def event(order_id, stage, **details):
            handle.publish_feedback(ExecuteAgent.Feedback(stage=stage,
                detail_json=json.dumps({"order_id": order_id, **details}, ensure_ascii=False)))

        status, error, detection_id = "COMPLETED", "", ""
        try:
            if cancelled():
                raise OrderCancelled("Agent cancelled before connecting tools")
            with WorkstationMCPClient(request.mcp_url, request.mcp_token,
                    timeout_s=self.config.get("scheduler", {}).get("result_timeout_s", 900)) as client:
                context = client.context()
                if context["order_id"] != order.order_id or context["zone"] != order.zone or context["requested"] != {line.sku: line.quantity for line in order.lines}:
                    raise OrderError("MCP context does not match the admitted order")
                event(order.order_id, "MCP_CONNECTED", transport="streamable-http",
                      endpoint=request.mcp_url, tools=[tool["function"]["name"] for tool in client.tools])
                agent = ProduceAgent(order, self.config["agent"], client, model=self.model,
                    event_sink=event, cancel_requested=cancelled)
                if getattr(request, "mode", "") == "select":
                    detection_id = agent.select_candidate()
                else:
                    agent.run()
        except OrderCancelled as exc:
            status, error = "CANCELLED", str(exc)
        except OrderPaused as exc:
            status, error = "PAUSED", str(exc)
        except Exception as exc:
            status, error = "FAILED", str(exc)
        finally:
            with self._lock:
                self._busy = False
                self._active_order = ""
            self.publish_status()
        result = ExecuteAgent.Result(status=status, error=error, detection_id=detection_id)
        if status == "CANCELLED" and handle.is_cancel_requested:
            handle.canceled()
        elif status in ("COMPLETED", "PAUSED"):
            handle.succeed()
        else:
            handle.abort()
        return result

    def accept_business(self, request):
        try:
            url = urlparse(request.mcp_url)
            valid = (url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
                and url.path == "/mcp" and not (url.username or url.password or url.query or url.fragment)
                and request.mcp_token and 0 < len(request.question.strip()) <= 2000)
        except ValueError:
            valid = False
        with self._lock:
            if not valid or self._business_busy or self.model is None or self._stopping.is_set():
                return GoalResponse.REJECT
            self._business_busy = True
        return GoalResponse.ACCEPT

    def execute_business(self, handle):
        from .business_agent import run_business_agent
        result = ExecuteBusiness.Result()
        try:
            with WorkstationMCPClient(handle.request.mcp_url, handle.request.mcp_token, timeout_s=120) as client:
                value = run_business_agent(handle.request.question, client, self.model,
                    self.config["agent"], lambda: handle.is_cancel_requested or self._stopping.is_set())
            result.status, result.result_json = "COMPLETED", json.dumps(value, ensure_ascii=False)
            handle.succeed()
        except OrderCancelled as exc:
            result.status, result.result_json = "CANCELLED", json.dumps({"error": str(exc)})
            if handle.is_cancel_requested:
                handle.canceled()
            else:
                handle.abort()
        except Exception as exc:
            result.status, result.result_json = "FAILED", json.dumps({"error": str(exc)}, ensure_ascii=False)
            handle.abort()
        finally:
            with self._lock:
                self._business_busy = False
        return result

    def close(self):
        self._stopping.set()
        self._business_server.destroy()
        self._server.destroy()
        self.destroy_node()
        if self.model is not None and hasattr(self.model, "close"):
            self.model.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(
        get_package_share_directory("produce_order_workstation")) / "config/workstation.yaml")
    args = parser.parse_args(remove_ros_args()[1:])
    rclpy.init()
    service = ProduceAgentService(yaml.safe_load(args.config.read_text()))
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(service)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        service._stopping.set()
        executor.shutdown()
        service.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
