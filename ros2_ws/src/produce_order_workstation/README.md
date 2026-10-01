# Produce order workstation

This package turns the existing O3DE Panda manipulation demo into a small,
testable order-picking workcell. The business input is a YAML order; the output
is a sequence of ROS 2 events and a JSON completion report. It reuses RAI's
manipulation service. Phase 2 adds a persistent ROS 2 Action order server and a
small desktop debug panel. The planner remains deterministic; an Agent can be
added above the same order boundary later.

## Scope and boundaries

| Module | Responsibility |
| --- | --- |
| `config/demo_order.yaml` | One sample order: one corn and one carrot to zone A. |
| `config/demo_scene.yaml` | Reproducible O3DE spawn positions for four receiving bins; products arrive one at a time. |
| `config/workstation.yaml` | Camera frames/topics, SKU aliases, reachable area, visible bin centers, tolerances, and motion limits. |
| `order.py` | ROS-independent validation, centroid parsing, source selection, and placement checks. |
| `runtime.py` | ROS 2 adapter: waits for interfaces, reads scene identity or perception, calls manipulation, emits events, runs the order state sequence. |
| `cli.py` | Command-line entry, scene preparation, JSON report, exit status. |
| `launch/workstation.launch.py` | O3DE benchmark, MoveIt, manipulation process, and order server; perception is optional. |
| `test/test_order.py` | Fast tests for order rules, position parsing, and safety checks. |
| `produce_order_interfaces/action/ExecuteOrder.action` | Typed order goal, progress feedback, and terminal result. |
| `order_server.py` | Persistent Action server; validates goals, owns one active order, handles cancellation, writes reports. |
| `debug_panel.py` | Temporary PyQt5 desktop client for order input, live progress, cancellation, and report path. |

Data path:

```text
order YAML -> validate capacity -> assign bins -> spawn receiving bins
           -> feed one item -> select source -> grab -> release above bin
           -> retract -> verify -> feed next item
                                      each transition -> /produce_orders/events
                                      final result    -> JSON report
```

For `--prepare-scene`, object positions and post-placement checks come from the
simulator's named TF entity frames (`position_source: scene_tf`). This is
intentional: the installed open-vocabulary vision model has confused corn and
carrot in this scene. A false class label must not count as an order success.
Without `--prepare-scene`, the tool uses RAI's camera-based position estimate
(`position_source: perception`), which is a learning path but not an
identity-safe production mode.

The event topic is `std_msgs/msg/String` with one UTF-8 JSON object per
message. Every object has `time_utc`, `order_id`, and `stage`; later fields
depend on the stage. Useful stages are `FEEDING`, `DETECTING`, `GRASPING`, `PLACING`,
`VERIFYING`, `ITEM_COMPLETED`, `ORDER_COMPLETED`, and `ORDER_FAILED`.
`FINAL_AUDIT` rechecks every placed item after the last motion, so a later
placement cannot silently eject an earlier one while leaving the order marked
complete.
Placement-related events also carry `slot_id` (`A1` through `A4`); `PLACING` and
`VERIFYING` include the measured `bin_position` in `panda_link0`.

## Run in the existing RAI environment

The base RAI repository is still required. Its downloaded O3DE demo assets,
MoveIt configuration, RAI packages, and model cache are not copied into this
package. Use the existing ROS Jazzy + `rai-jazzy` Conda environment and source
the repository's `install/setup.bash` overlay. On this host, the project root
is `/home/wang/rai_embodied_project`.

```bash
cd /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo
source /opt/ros/jazzy/setup.bash
source /home/wang/miniconda3/etc/profile.d/conda.sh
conda activate rai-jazzy
colcon build --packages-select produce_order_interfaces produce_order_workstation --symlink-install
source /home/wang/rai_embodied_project/install/setup.bash
source install/setup.bash
ros2 launch produce_order_workstation workstation.launch.py
```

The launch command stays open. In a second terminal with the same sourced
environment:

```bash
ros2 run produce_order_workstation run_order --dry-run
ros2 run produce_order_workstation run_order --prepare-scene
ros2 topic echo /produce_orders/events
```

`--prepare-scene` spawns four orange `toy_box` receiving bins into the empty
`RoboticManipulationBenchmark` level. During execution it spawns each requested
product at a safe feeder position only after the previous item leaves that
position; the old all-at-once layout let corn fall off the table. A1–A4 are
arranged left to right on the front of the table. Their
centers in `panda_link0` are `(0.50, -0.48)`, `(0.57, -0.16)`,
`(0.57, 0.16)`, and `(0.50, 0.48)`. These are physical bin centers, not
invisible abstract slots. Scene setup checks that all bins exist at their
configured positions; before and after
each placement the active bin is checked again. Verification uses the named
product entity and the bin's rectangular interior footprint; a circular
center-distance check incorrectly rejected items resting in the corners.
The gripper releases above
the box rim and retracts before verification; leaving it inside the box caused
collisions in an earlier test. Do not run scene setup
against a manually prepared scene unless you intend to add objects. The
default report is at
`~/.local/share/produce_order_workstation/reports/produce-demo-001.json`;
pass `--report /absolute/path.json` to override it. A non-completed order exits
with status 1 and still writes the failure report. The visual demo is not a
substitute for verification: inspect `ORDER_COMPLETED` and the report.

If the model weights are already cached and Hugging Face is unreachable, set
`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` before launching. This only affects
the model loader, not ROS communication.

## Phase 2: persistent orders and debug panel

The launch file now starts `/produce_orders/execute`, an Action server. Send a
goal with an order ID, zone `A`, `OrderLine` entries for `corn`, `carrot`,
`apple`, or `tomato`, and `prepare_scene=true`. The current demo limit is four
pieces per box, independent of SKU, for up to 16 pieces across four boxes.
The planner balances items over the fewest boxes needed and chooses one of four
drop points in each box. Scene preparation spawns only the bins; each product
is spawned at the feeder just before its grasp. The
feedback fields are `stage`, `sku`,
`completed`, `total`, and `detail_json`. The result fields are `status`,
`completed`, `requested`, `error`, and `report_path`. The JSON event topic and
report from phase 1 remain available. Goal validation is shared with YAML
orders; only one order runs at a time, and a reused report ID is rejected.

For the simple desktop client, open another terminal and source ROS Jazzy and
this worktree's overlay, then use the system Python (which already has PyQt5):

```bash
cd /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 run produce_order_workstation debug_panel
```

No new packages or model downloads were needed for this phase. The panel is a
learning/debugging surface, not the final business UI. It deliberately shows
the order input, current stage, progress, event details, and report location;
it does not claim Agent integration. Its form previews the planned contents of
each box and disables submission when the count exceeds four per box across
the four-box layout. The current order state and previous result are displayed
separately, so an invalid new input cannot appear to be a completed order.

With `prepare_scene=true`, order execution uses named simulator entity TF to
check item identity. Therefore `workstation.launch.py` defaults to
`enable_perception:=false`, avoiding an unnecessary vision-model load. For
manual-scene camera experiments, launch with `enable_perception:=true`; model
weights must already be available. Do not run the old `run_order` CLI and an
Action order concurrently against the same robot.

Cancellation is checked before motion and between items; it does **not**
interrupt a grasp or placement in flight. A cancellation result is
`CANCELLED`, with the number of completed items recorded. A ROS service timeout
or simulator exit is a failure, not a successful order. In particular, if O3DE
closes, verify `/clock` and the `world`/`odom` TF before trying a new order.
The order server may stay alive while O3DE restarts: scene preparation now
discards only entities that O3DE confirms no longer exist, then spawns a fresh
scene. A completed or failed order still reserves its order ID in the report
directory; the debug panel generates microsecond-resolution IDs to avoid an
immediate retry accidentally reusing the same ID.

Live checks on 2026-10-01 completed the user's six-piece mixed order (`6/6`,
three items in each of two boxes) and an eight-apple order (`8/8`, four apples
in each of two boxes). All eight apples remained inside the assigned bin
footprints at the final check. Unit tests cover capacity allocation, sequential
feeding, Action validation, cancellation before motion, and the
release/retract sequence. Four of every SKU in one box has not been separately
stress-tested; the item-count limit is a demo rule, not a measured industrial
payload rating.

## Safety and current limitations

- The order is rejected before motion if the SKU, quantity, zone, or slot
  capacity is invalid.
- Only detected points inside the configured work area and outside all zone
  slots can be selected as sources.
- A service timeout is treated as an unknown physical state, so the order stops
  instead of attempting the next item.
- After release, verification requires that the *same named simulator entity*
  has moved near the assigned slot and left its source, when the demo scene was
  prepared. In manual-scene mode this check is perception-based and cannot
  prove the item's identity.
- No inventory persistence, retry policy, human approval, or LLM/Agent
  reasoning is implemented in phase 1. These are future stages, not claimed
  capabilities.
