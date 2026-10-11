#!/usr/bin/env python3
"""Save one live camera frame for inspection. No orders or motion."""
from pathlib import Path
import time
import cv2
from cv_bridge import CvBridge
import rclpy
from sensor_msgs.msg import Image


def main():
    rclpy.init()
    node = rclpy.create_node("workstation_camera_inspection")
    frames = []
    node.create_subscription(Image, "/color_image5", frames.append, 1)
    try:
        deadline = time.monotonic() + 10
        while not frames and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.1)
        if not frames:
            raise TimeoutError("Camera did not publish")
        folder = Path.home() / ".local/share/produce_order_workstation/perception"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"inspection-{time.time_ns()}.jpg"
        if not cv2.imwrite(str(path), CvBridge().imgmsg_to_cv2(frames[-1], desired_encoding="bgr8")):
            raise RuntimeError("Could not save camera image")
        print(path)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
