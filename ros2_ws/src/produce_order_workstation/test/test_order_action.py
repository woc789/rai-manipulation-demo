from pathlib import Path
from unittest.mock import MagicMock

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine as OrderLineMsg
import yaml

from produce_order_workstation.order import Order, OrderLine, validate_order
from produce_order_workstation.order_server import order_from_goal
from produce_order_workstation.runtime import Workstation


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())


def test_action_goal_uses_same_validation_as_yaml_order():
    goal = ExecuteOrder.Goal()
    goal.order_id = "action-001"
    goal.zone = "A"
    line = OrderLineMsg()
    line.sku = "corn"
    line.quantity = 1
    goal.lines = [line]
    order = order_from_goal(goal)
    validate_order(order, CONFIG)
    assert order == Order("action-001", "A", (OrderLine("corn", 1),))


def test_cancel_before_first_item_issues_no_motion():
    workstation = Workstation(MagicMock(), CONFIG, cancel_requested=lambda: True)
    workstation.move = MagicMock()
    result = workstation.run(Order("cancel-001", "A", (OrderLine("corn", 1),)))
    assert result["status"] == "CANCELLED"
    assert result["completed"] == 0
    assert result["events"][-1]["stage"] == "ORDER_CANCELLED"
    workstation.move.assert_not_called()
