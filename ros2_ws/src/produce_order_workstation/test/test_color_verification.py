from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from cv_bridge import CvBridge

from produce_order_workstation.color_verification import (
    ColorVerifiedPointCloud,
    classify_mask,
    install_color_verification,
)
from rai_perception.components.gripping_points import PointCloudFromSegmentation
from rai_perception.components.gripping_points import PointCloudFromSegmentationConfig


def sample(bgr):
    image = np.full((200, 200, 3), (30, 60, 210), np.uint8)
    image[80:100, 80:100] = bgr
    mask = np.zeros((200, 200), np.uint8)
    mask[80:100, 80:100] = 255
    return image, mask


@pytest.mark.parametrize(
    "bgr,label",
    [
        ((100, 150, 200), "apple"),
        ((30, 60, 210), "tomato"),
        ((20, 180, 20), None),
        ((220, 220, 220), None),
        ((5, 5, 5), None),
    ],
)
def test_color_rules_are_conservative(bgr, label):
    image, mask = sample(bgr)
    assert classify_mask(image, mask, {})["label"] == label


def test_background_does_not_decide_mask_color():
    image, mask = sample((100, 150, 200))
    assert classify_mask(image, mask, {})["label"] == "apple"
    image[mask == 0] = (20, 180, 20)
    assert classify_mask(image, mask, {})["label"] == "apple"


def test_small_empty_and_oversized_masks_fail_closed():
    image, mask = sample((100, 150, 200))
    for invalid in (np.zeros_like(mask), np.full_like(mask, 255)):
        assert classify_mask(image, invalid, {})["label"] is None
    with pytest.raises(ValueError):
        classify_mask(image, mask[:10], {})


def test_mixed_colors_need_dominance():
    image, mask = sample((100, 150, 200))
    image[80:100, 90:100] = (30, 60, 210)
    assert classify_mask(image, mask, {})["label"] is None


def test_asset_calibration_still_requires_both_support_and_margin():
    # Explicit profile threshold must never become an arbitrary top-N choice.
    image, mask = sample((100, 150, 200))
    image[80:100, 90:100] = (30, 60, 210)
    assert (
        classify_mask(image, mask, {"min_support": 0.52, "min_margin": 0.2})["label"]
        is None
    )


@pytest.mark.parametrize("sku,expected", [("apple", 1), ("tomato", 1), (None, 2)])
@pytest.mark.parametrize("encoding", ["rgba8", "rgb8", "bgr8"])
def test_pipeline_filters_masks_before_projection_without_changing_image(
    monkeypatch, sku, expected, encoding
):
    image, apple_mask = sample((100, 150, 200))
    image[120:140, 120:140] = (30, 60, 210)
    tomato_mask = np.zeros_like(apple_mask)
    tomato_mask[120:140, 120:140] = 255
    bridge = CvBridge()
    masks = [
        bridge.cv2_to_imgmsg(m, encoding="mono8") for m in (apple_mask, tomato_mask)
    ]
    response = SimpleNamespace(masks=masks)
    monkeypatch.setattr(
        PointCloudFromSegmentation, "_await_response", lambda *args: response
    )
    pipeline = object.__new__(ColorVerifiedPointCloud)
    pipeline._color_sku = sku
    encoded = (
        image
        if encoding == "bgr8"
        else cv2.cvtColor(
            image, cv2.COLOR_BGR2RGBA if encoding == "rgba8" else cv2.COLOR_BGR2RGB
        )
    )
    pipeline.last_image = bridge.cv2_to_imgmsg(encoded, encoding=encoding)
    pipeline.last_diagnostics = {}
    pipeline.color_policy = {}
    pipeline.check_cancel = lambda: None
    original = bytes(pipeline.last_image.data)
    result = pipeline._await_response(None)
    assert len(result.masks) == expected
    assert bytes(pipeline.last_image.data) == original
    if sku:
        evidence = pipeline.last_diagnostics["color_verification"]
        assert [r["label"] for r in evidence] == ["apple", "tomato"]
        assert sum(r["accepted"] for r in evidence) == 1


def test_disabled_gate_leaves_original_component_unchanged():
    component = object()
    assert install_color_verification(component, {"enabled": False}, {}) is component


def test_enabled_gate_never_silently_bypasses_unsupported_component():
    with pytest.raises(TypeError):
        install_color_verification(object(), {"enabled": True}, {})


@pytest.mark.parametrize("value", [-1, float("nan"), 2])
def test_invalid_color_policy_is_rejected(value):
    with pytest.raises(ValueError):
        ColorVerifiedPointCloud(
            color_policy={"profile": "brown_apple_red_tomato", "min_support": value},
            aliases={},
            connector=None,
            camera_topic="rgb",
            depth_topic="depth",
            camera_info_topic="info",
            source_frame="camera",
            target_frame="base",
            config=PointCloudFromSegmentationConfig(),
        )
