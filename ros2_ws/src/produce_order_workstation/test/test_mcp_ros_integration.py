"""Real MCP → ROS service / ExecuteMotion Action in explicit isolated domain 71.

The model, camera and robot are substitutes. Never connect to the user's workcell.
"""

import json
import os
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
import rclpy
from rclpy.action import ActionServer
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_srvs.srv import Trigger
import yaml

from produce_order_interfaces.action import ExecuteMotion
from produce_order_workstation.order import Order, OrderLine
from produce_order_workstation.runtime import Workstation
from rai.communication.ros2.ros_async import get_future_result


pytestmark = pytest.mark.skipif(
    os.getenv("PRODUCE_ROS_INTEGRATION") != "1" or os.getenv("ROS_DOMAIN_ID") != "71",
    reason="Requires explicit isolated ROS domain 71; never target the user's workcell")


def test_model_mcp_service_and_real_motion_action_chain():
    rclpy.init()
    host = Node("isolated_mcp_host")
    backend = Node("isolated_mcp_capabilities")
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(host)
    executor.add_node(backend)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    steps = []

    def vision(_request, response):
        response.success = True
        response.message = json.dumps({"position": [.4, .1, .04]})
        return response

    backend.create_service(Trigger, "/isolated_mcp/perception", vision)

    def motion(handle):
        steps.append(handle.request.step)
        handle.publish_feedback(ExecuteMotion.Feedback(stage="ISOLATED_EXECUTION"))
        handle.succeed()
        return ExecuteMotion.Result(success=True)

    action = ActionServer(backend, ExecuteMotion, "/produce_orders/execute_motion", motion)
    vision_client = host.create_client(Trigger, "/isolated_mcp/perception")
    config = yaml.safe_load((Path(__file__).parents[1] / "config/workstation.yaml").read_text())
    station = Workstation(SimpleNamespace(node=host), config)
    station._scene_prepared = True
    station._active_order_id = "isolated-mcp-order"
    order = Order(station._active_order_id, "A", (OrderLine("corn", 1),))

    def perceive(_order, _sku):
        response = get_future_result(vision_client.call_async(Trigger.Request()), timeout_sec=5)
        assert response is not None and response.success
        point = tuple(json.loads(response.message)["position"])
        station._visual_candidates["isolated_visual"] = ("corn", point)
        return {"detections": [{"detection_id": "isolated_visual", "position": list(point)}]}

    def pick(_order, detection_id):
        point = station._visual_candidates[detection_id][1]
        for step in ("grab", "lift", "place", "retreat"):
            station.move(point, step=step)
        station._completed_by_sku["corn"] = 1
        station._placed_entities.append(("corn", None, 0, point))
        return {"placed": detection_id}

    station.get_object_positions = perceive
    station.pick_and_place = pick
    station.finish_order = lambda _: {"status": "verified"}
    calls = iter([("get_object_positions", {"sku": "corn"}),
                  ("pick_and_place", {"detection_id": "isolated_visual"}),
                  ("finish_order", {})])

    def model_response(**kwargs):
        assert len(kwargs["tools"]) == 3
        name, arguments = next(calls)
        tool = SimpleNamespace(id=name, function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments)))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[tool]))])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=model_response)))
    try:
        assert vision_client.wait_for_service(timeout_sec=5)
        result = station.run(order, model=model)
        assert result["status"] == "COMPLETED", result["error"]
        assert result["completed"] == result["requested"] == 1
        assert steps == ["grab", "lift", "place", "retreat"]
        events = station.events
        assert sum(event["stage"] == "MCP_TOOL_CALL" for event in events) == 3
        assert sum(event["stage"] == "MOTION_STAGE" for event in events) == 4
        assert station._completed_by_sku == {"corn": 1}
    finally:
        executor.shutdown(timeout_sec=5)
        thread.join(timeout=5)
        action.destroy()
        if station._motion_client is not None:
            station._motion_client.destroy()
        host.destroy_node()
        backend.destroy_node()
        rclpy.try_shutdown()
