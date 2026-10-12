"""Order host ROS Action client: no Agent construction or model dependency."""

import json
import time

from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from rai.communication.ros2.ros_async import get_future_result
from produce_order_interfaces.action import ExecuteAgent
from produce_order_interfaces.msg import OrderLine

from .order import OrderCancelled, OrderError, OrderPaused


class AgentDispatcher:
    def __init__(self, node, config, event_sink, cancel_requested):
        self.client = ActionClient(node, ExecuteAgent, "/produce_agent/execute")
        self.config = config.get("scheduler", {})
        self.event = event_sink
        self.cancel_requested = cancel_requested
        self.node = node
        self.state = "UNKNOWN"
        self.error = ""
        self._status_subscription = node.create_subscription(String, "/produce_agent/status",
            self._status_received, QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def _status_received(self, message):
        try:
            status = json.loads(message.data)
            self.state, self.error = status["state"], status.get("error", "")
        except (ValueError, KeyError):
            self.state = "UNKNOWN"

    def ready(self):
        return self.client.server_is_ready() and self.state not in ("MODEL_UNAVAILABLE", "BUSY")

    def execute(self, order, endpoint, token):
        if not self.client.wait_for_server(timeout_sec=5):
            raise OrderError("Agent service unavailable; check produce_agent_service startup")
        goal = ExecuteAgent.Goal(order_id=order.order_id, zone=order.zone,
            lines=[OrderLine(sku=line.sku, quantity=line.quantity) for line in order.lines],
            mcp_url=endpoint, mcp_token=token)

        def feedback(message):
            details = json.loads(message.feedback.detail_json)
            details.pop("order_id", None)
            self.event(order.order_id, message.feedback.stage, **details)

        handle = get_future_result(self.client.send_goal_async(goal, feedback_callback=feedback),
                                  timeout_sec=self.config.get("accept_timeout_s", 15))
        if handle is None:
            raise OrderError("Agent Action acceptance timed out; do not repeat automatically")
        if not handle.accepted:
            raise OrderError("Agent rejected task: busy, stopping, model unavailable or invalid capability")
        result = handle.get_result_async()
        deadline = time.monotonic() + self.config.get("result_timeout_s", 900)
        cancelled = False
        while not result.done():
            if self.cancel_requested() and not cancelled:
                handle.cancel_goal_async()
                cancelled = True
            if time.monotonic() > deadline:
                handle.cancel_goal_async()
                raise OrderError("Agent Action timed out; check execution state before retrying")
            time.sleep(.05)
        wrapped = result.result()
        if wrapped.result.status == "PAUSED":
            raise OrderPaused(wrapped.result.error)
        if wrapped.result.status == "CANCELLED":
            raise OrderCancelled(wrapped.result.error)
        if wrapped.status != GoalStatus.STATUS_SUCCEEDED or wrapped.result.status != "COMPLETED":
            raise OrderError(wrapped.result.error or "Agent Action failed")

    def close(self):
        self.client.destroy()
        self.node.destroy_subscription(self._status_subscription)
