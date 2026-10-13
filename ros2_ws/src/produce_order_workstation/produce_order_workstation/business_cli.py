"""Small explicit ROS business client. Approval is never implicit."""
import argparse
import json

import rclpy
from produce_order_interfaces.srv import BusinessCommand


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command")
    parser.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    args = parser.parse_args()
    json.loads(args.arguments)
    rclpy.init()
    node = rclpy.create_node("produce_business_cli")
    try:
        client = node.create_client(BusinessCommand, "/produce_orders/business")
        if not client.wait_for_service(timeout_sec=5):
            raise SystemExit("Business service unavailable")
        future = client.call_async(BusinessCommand.Request(command=args.command, arguments_json=args.arguments))
        rclpy.spin_until_future_complete(node, future, timeout_sec=210)
        if not future.done():
            raise SystemExit("Result unconfirmed: inspect requests/state before retrying")
        response = future.result()
        print(json.dumps(json.loads(response.result_json), ensure_ascii=False, indent=2))
        if not response.success:
            raise SystemExit(1)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
