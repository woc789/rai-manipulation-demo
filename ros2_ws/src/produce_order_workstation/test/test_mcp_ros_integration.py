"""Real MCP → ROS service / ExecuteMotion Action in explicit isolated domain 71.

The model, camera and robot are substitutes. Never connect to the user's workcell.
"""

import json
import os
import multiprocessing
import time
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

from action_msgs.msg import GoalStatus
from produce_order_interfaces.action import ExecuteAgent, ExecuteMotion
from produce_order_interfaces.msg import OrderLine as OrderLineMsg
from produce_order_workstation.order import Order, OrderLine
from produce_order_workstation.runtime import Workstation
from produce_order_workstation.agent_service import ProduceAgentService
from produce_order_workstation.agent_dispatch import AgentDispatcher
from produce_order_workstation.mcp_server import WorkstationMCPServer
from rai.communication.ros2.ros_async import get_future_result


pytestmark = pytest.mark.skipif(
    os.getenv("PRODUCE_ROS_INTEGRATION") != "1" or os.getenv("ROS_DOMAIN_ID") != "71",
    reason="Requires explicit isolated ROS domain 71; never target the user's workcell")


def child_agent_runtime(config, stop, ready, count, model_gate):
    """A different OS process: deterministic model only, no cloud request."""
    choices = [("get_object_positions", {"sku": "corn"}),
               ("pick_and_place", {"detection_id": "isolated_visual"}), ("finish_order", {})]
    index = [0]
    def response(**_):
        with count.get_lock():
            count.value += 1
        assert model_gate.wait(10)
        name, arguments = choices[index[0] % len(choices)]
        index[0] += 1
        tool = SimpleNamespace(id=str(index[0]), function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments)))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[tool]))])
    rclpy.init()
    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=response)))
    service = ProduceAgentService(config, model=model)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(service)
    ready.set()
    try:
        while not stop.is_set():
            executor.spin_once(timeout_sec=.1)
    finally:
        service._stopping.set()
        executor.shutdown(timeout_sec=5)
        service.close()
        rclpy.try_shutdown()


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
    context = multiprocessing.get_context("spawn")
    stop, ready = context.Event(), context.Event()
    count, model_gate = context.Value("i", 0), context.Event()
    model_gate.set()
    process = context.Process(target=child_agent_runtime, args=(config, stop, ready, count, model_gate))
    process.start()
    gateway = WorkstationMCPServer(station).__enter__()
    station._mcp_server = gateway
    station._agent_dispatcher = AgentDispatcher(host, config, station.event,
                                               lambda: station.cancel_requested())
    try:
        assert ready.wait(15)
        assert process.pid != os.getpid() and process.is_alive()
        assert count.value == 0  # Online/idle is not a model invocation.
        assert gateway.order is None
        assert vision_client.wait_for_service(timeout_sec=5)
        result = station.run(order)
        assert result["status"] == "COMPLETED", result["error"]
        assert result["completed"] == result["requested"] == 1
        assert steps == ["grab", "lift", "place", "retreat"]
        events = station.events
        assert sum(event["stage"] == "MCP_TOOL_CALL" for event in events) == 3
        assert sum(event["stage"] == "MOTION_STAGE" for event in events) == 4
        assert station._completed_by_sku == {"corn": 1}
        assert gateway.order is None and process.is_alive()
        # The same separate Agent process and gateway handle a second run.
        station._completed_by_sku = {}
        station._placed_entities = []
        second = Order("isolated-mcp-second", "A", (OrderLine("corn", 1),))
        result = station.run(second)
        assert result["status"] == "COMPLETED", result["error"]
        assert steps == ["grab", "lift", "place", "retreat"] * 2
        assert process.is_alive()
        assert gateway.order is None
        assert count.value == 6
        # Hold the model response to exercise real busy rejection and cancellation.
        station._completed_by_sku = {}
        model_gate.clear()
        third = Order("isolated-cancel", "A", (OrderLine("corn", 1),))
        with gateway.order_session(third):
            goal = ExecuteAgent.Goal(order_id=third.order_id, zone="A",
                lines=[OrderLineMsg(sku="corn", quantity=1)],
                mcp_url=gateway.url, mcp_token=gateway.token)
            handle = get_future_result(station._agent_dispatcher.client.send_goal_async(goal), timeout_sec=5)
            assert handle is not None and handle.accepted
            deadline = time.monotonic() + 5
            while count.value != 7 and time.monotonic() < deadline:
                time.sleep(.02)
            assert count.value == 7
            busy = get_future_result(station._agent_dispatcher.client.send_goal_async(goal), timeout_sec=5)
            assert busy is not None and not busy.accepted
            cancellation = get_future_result(handle.cancel_goal_async(), timeout_sec=5)
            assert cancellation is not None and cancellation.goals_canceling
            model_gate.set()
            result = get_future_result(handle.get_result_async(), timeout_sec=5)
            assert result is not None and result.status == GoalStatus.STATUS_CANCELED
            assert result.result.status == "CANCELLED"
        assert steps == ["grab", "lift", "place", "retreat"] * 2
    finally:
        executor.shutdown(timeout_sec=5)
        thread.join(timeout=5)
        action.destroy()
        gateway.close()
        station._agent_dispatcher.close()
        stop.set()
        model_gate.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        assert process.exitcode == 0
        if station._motion_client is not None:
            station._motion_client.destroy()
        host.destroy_node()
        backend.destroy_node()
        rclpy.try_shutdown()
