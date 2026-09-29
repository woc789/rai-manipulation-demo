# Phase 1 implementation record (2026-09-29)

## Goal and result

Establish a repeatable produce-order business flow on the existing ROS 2/O3DE
Panda demo. The input is an order, not an LLM prompt. A single run must be able
to spawn a known scene, execute each order line in sequence, publish observable
state transitions, and report verified completion or a failure reason.

The final live run used `produce-demo-001` (corn ×1, carrot ×1, zone A) and
finished with `ORDER_COMPLETED`, `completed: 2`, `requested: 2`. The report is
written outside the Git checkout under
`~/.local/share/produce_order_workstation/reports/`.

## Implementation decisions

1. **A separate `ament_python` package.** The new package lives in the demo's
   ROS 2 workspace and is exposed through `ros2 run` and `ros2 launch`. It
   depends on the existing RAI and O3DE assets rather than copying them.
2. **Configuration instead of hard-coded orders.** YAML defines the order,
   camera and frame names, workspace, destination slots, timing, and the
   reproducible scene. `load_order()` validates SKU, quantity, unique lines,
   destination zone, and slot capacity before any motion.
3. **A ROS-independent rules layer.** `order.py` contains spatial selection and
   placement checks, so failures can be unit-tested without a running robot.
4. **A ROS 2 adapter.** `runtime.py` waits for interfaces, publishes JSON
   events on `/produce_orders/events` (`std_msgs/msg/String`), uses
   `rai_interfaces/srv/ManipulatorMoveTo`, and runs a sequential
   detect/locate → grasp → place → verify flow. A timeout or failed service
   response stops the order.
5. **Simulator identity for the controlled scene.** The first live run showed
   open-vocabulary perception swapped corn and carrot locations. It had moved
   the wrong item and could even temporarily accept that false class label as
   success. For `--prepare-scene`, the workcell now retains the named
   `SpawnedEntity` objects and uses their TF poses both to select a SKU and
   verify the *same entity* after release. Manual scenes still use camera-based
   perception and should be treated as an experimental path.
6. **Sequential model initialization.** The original perception launcher
   initialized GroundingDINO and SAM2 concurrently. On this host, SAM2 then
   failed while transferring a `meta tensor` to the GPU and never exposed
   `/segmentation`. Initializing them in sequence fixed startup and allows
   initialization exceptions to surface instead of silently omitting a
   service. Cached weights can be used with `HF_HUB_OFFLINE=1` and
   `TRANSFORMERS_OFFLINE=1` if Hugging Face is unreachable.

## Verification and scope

- `colcon build --packages-select produce_order_workstation --symlink-install`:
  succeeded.
- `python -m pytest .../produce_order_workstation/test/test_order.py -q`:
  6 tests passed. These cover validation, centroid parsing, spatial checks,
  and the prepared-scene identity path.
- Live O3DE + MoveIt run: both object motions returned success; named-entity
  placement checks passed; the final JSON report records 2/2 complete.
- `ruff` was not installed in the `rai-jazzy` Conda environment, so the Ruff
  check could not run. This is a tooling gap, not a passing lint result.

Phase 1 does **not** implement an Agent, natural-language order intake,
inventory database, retries, multi-order scheduling, or trustworthy semantic
classification in arbitrary scenes. Those boundaries are intentional so the
next stage can be built and evaluated against a stable order interface.
