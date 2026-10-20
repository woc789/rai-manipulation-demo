"""Mask-only color evidence for this workcell's brown-apple/red-tomato assets.

Not a universal fruit classifier. Never reads entity IDs, poses or expected counts.
"""

from math import isfinite

import cv2
import numpy as np

from rai_perception.components.gripping_points import PointCloudFromSegmentation


def classify_mask(bgr, mask, policy):
    if bgr.ndim != 3 or bgr.shape[2] != 3 or mask.shape != bgr.shape[:2]:
        raise ValueError("Color verification requires aligned BGR image and 2D mask")
    interior = cv2.erode((mask > 0).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    area = int(interior.sum())
    result = dict(
        label=None, interior_pixels=area, apple_fraction=0.0, tomato_fraction=0.0
    )
    if area < policy.get("min_pixels", 50) or area > mask.size * policy.get(
        "max_area_fraction", 0.02
    ):
        return dict(result, reason="mask_size_out_of_range")
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    pixels = hsv[interior]
    # Exclude weak color, deep shadow and specular highlights; do not count
    # the green stem as brown or red evidence.
    valid = pixels[(pixels[:, 1] >= 50) & (pixels[:, 2] >= 40) & (pixels[:, 2] <= 250)]
    if len(valid) < policy.get("min_pixels", 50) or len(valid) < area * 0.3:
        return dict(result, reason="insufficient_color_evidence")
    h, s = valid[:, 0], valid[:, 1]
    apple = float(np.mean((h >= 8) & (h <= 30) & (s >= 60) & (s <= 170)))
    tomato = float(np.mean(((h <= 10) | (h >= 170)) & (s >= 175)))
    result.update(
        apple_fraction=apple,
        tomato_fraction=tomato,
        median_hsv=np.median(valid, axis=0).tolist(),
        valid_pixels=len(valid),
    )
    support = policy.get("min_support", 0.55)
    margin = policy.get("min_margin", 0.20)
    if apple >= support and apple - tomato >= margin:
        result["label"] = "apple"
    elif tomato >= support and tomato - apple >= margin:
        result["label"] = "tomato"
    result["reason"] = "color_confirmed" if result["label"] else "ambiguous_color"
    return result


class ColorVerifiedPointCloud(PointCloudFromSegmentation):
    """Reuse detection/SAM/depth/TF; filter SAM masks before 3D projection."""

    def __init__(self, *, color_policy, aliases, **kwargs):
        super().__init__(**kwargs)
        self.color_policy = color_policy
        if color_policy.get("profile") != "brown_apple_red_tomato":
            raise ValueError("Unsupported workcell color profile")
        for key, default, lower in (
            ("min_support", 0.55, 0.5),
            ("min_margin", 0.2, 0.1),
        ):
            value = float(color_policy.get(key, default))
            if not isfinite(value) or not lower <= value <= 1:
                raise ValueError(f"Invalid color verification {key}")
        self.color_aliases = {
            alias.strip().lower(): sku
            for sku in ("apple", "tomato")
            for alias in aliases.get(sku, [])
        }
        self._color_sku = None

    def run(self, object_name):
        self._color_sku = self.color_aliases.get(object_name.strip().lower())
        try:
            points = super().run(object_name)
            evidence = self.last_diagnostics.get("color_verification", [])
            if evidence and not any(item["accepted"] for item in evidence):
                self.last_diagnostics["empty_stage"] = "color_verification"
            return points
        finally:
            self._color_sku = None

    def _await_response(self, future):
        response = super()._await_response(future)
        if response is None or not hasattr(response, "masks") or not self._color_sku:
            return response
        from cv_bridge import CvBridge

        bridge = CvBridge()
        bgr = bridge.imgmsg_to_cv2(self.last_image, desired_encoding="bgr8")
        kept, evidence = [], []
        for index, message in enumerate(response.masks):
            self.check_cancel()
            mask = bridge.imgmsg_to_cv2(message, desired_encoding="mono8")
            result = classify_mask(bgr, mask, self.color_policy)
            accepted = result["label"] == self._color_sku
            evidence.append(
                dict(
                    result,
                    mask_index=index,
                    requested=self._color_sku,
                    accepted=accepted,
                )
            )
            if accepted:
                kept.append(message)
        self.last_diagnostics["color_verification"] = evidence
        self.last_diagnostics["raw_mask_count"] = len(response.masks)
        response.masks = kept
        return response


def install_color_verification(pipeline, policy, aliases):
    if not policy.get("enabled", False):
        return pipeline
    if not isinstance(pipeline, PointCloudFromSegmentation):
        raise TypeError(
            "Enabled color verification requires PointCloudFromSegmentation"
        )
    return ColorVerifiedPointCloud(
        color_policy=policy,
        aliases=aliases,
        **{
            name: getattr(pipeline, name)
            for name in (
                "connector",
                "camera_topic",
                "depth_topic",
                "camera_info_topic",
                "source_frame",
                "target_frame",
                "conversion_ratio",
                "config",
            )
        },
    )
