"""Real compiled C++ BT + ROS transport; fake skills, isolated domain only."""
import os
from pathlib import Path
import subprocess
from threading import Thread
import time

import pytest
import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from produce_order_interfaces.action import RunFulfillment
from produce_order_interfaces.srv import FulfillmentStep
from produce_order_workstation.fulfillment_plan import compile_tree, context_digest


pytestmark = pytest.mark.skipif(os.getenv("PRODUCE_ROS_INTEGRATION") != "1" or os.getenv("ROS_DOMAIN_ID") != "71",
    reason="Explicit isolated DDS domain 71 required")


def wait(predicate, timeout=12):
    deadline = time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:
            raise AssertionError("BT transport timeout")
        time.sleep(.02)


@pytest.mark.parametrize("mode,expected", [("success","COMPLETED"),("planned","COMPLETED"),("empty","FAILED"),
                                         ("uncertain","FAILED"),("pause","PAUSED"),("cancel","CANCELLED")])
def test_compiled_tree_and_async_capability_protocol(mode, expected):
    workspace = Path(__file__).resolve().parents[3]
    binary = workspace/"install/produce_order_bt/lib/produce_order_bt/fulfillment_tree"
    if not binary.exists():
        pytest.fail("Build produce_order_bt before integration test")
    process = subprocess.Popen([str(binary)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    rclpy.init()
    node = Node("isolated_bt_skill_test")
    state = {"selected":0,"picked":0,"audit":0,"sequence":0,"in_pick":False,"targets":[]}
    def capability(request, response):
        assert request.sequence == state["sequence"]
        state["sequence"] += 1
        state["targets"].append((request.command, request.target_sku))
        if request.command == "select":
            state["selected"] += 1
            response.status = "RETRY" if mode == "empty" else "SUCCESS"
            response.detection_id = "v1"
            response.remaining = 2-state["picked"] if mode == "planned" else 1
            response.reason = "No visual candidate" if mode == "empty" else ""
        elif request.command == "pick":
            state["in_pick"] = True
            time.sleep(.7 if mode=="cancel" else .04)
            state["picked"] += 1
            response.status = {"uncertain":"FAILED","pause":"PAUSED","cancel":"CANCELLED"}.get(mode,"SUCCESS")
            response.reason = mode
            response.remaining = 2-state["picked"] if mode == "planned" else 0
        else:
            state["audit"] += 1
            response.status = "SUCCESS"
            response.remaining = 0
        return response
    service = node.create_service(FulfillmentStep,"/produce_orders/internal/step",capability,
                                  callback_group=ReentrantCallbackGroup())
    client = ActionClient(node,RunFulfillment,"/produce_orders/internal/run_tree")
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    thread = Thread(target=executor.spin,daemon=True)
    thread.start()
    try:
        assert client.wait_for_server(timeout_sec=8)
        xml = ""
        if mode == "planned":
            context = {"remaining": {"corn": 1, "apple": 1},
                       "limits": {"max_visual_attempts_per_item": 3}}
            plan = {"version": 1, "context_digest": context_digest(context),
                    "items": [{"sku": "apple", "visual_attempts": 2},
                              {"sku": "corn", "visual_attempts": 3}]}
            xml = compile_tree(plan, context)
        accepted = client.send_goal_async(RunFulfillment.Goal(session_id="isolated",max_attempts=3,
                                                               plan_xml=xml))
        wait(accepted.done)
        handle = accepted.result()
        assert handle.accepted
        result = handle.get_result_async()
        if mode=="cancel":
            wait(lambda:state["in_pick"])
            cancel = handle.cancel_goal_async()
            wait(cancel.done)
        wait(result.done)
        assert result.result().result.status == expected, result.result().result.error
        if mode=="empty":
            assert state["selected"] == 3 and state["picked"] == 0
        elif mode=="success":
            assert state["picked"] == state["audit"] == 1
        elif mode=="planned":
            assert state["picked"] == 2 and state["audit"] == 1
            assert state["targets"] == [("select", "apple"), ("pick", "apple"),
                                         ("select", "corn"), ("pick", "corn"), ("audit", "")]
        elif mode in ("uncertain","pause"):
            assert state["picked"] == 1 and state["audit"] == 0
    finally:
        executor.shutdown(timeout_sec=3)
        thread.join(timeout=3)
        client.destroy(); node.destroy_service(service); node.destroy_node(); rclpy.try_shutdown()
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
