# Produce order workstation — phase 1

This package turns the existing O3DE Panda manipulation demo into a small,
testable order-picking workcell. The business input is a YAML order; the output
is a sequence of ROS 2 events and a JSON completion report. It reuses RAI's
manipulation service. Phase 1 deliberately keeps the planner deterministic; an
Agent can be added above the same order boundary later.

## Scope and boundaries

| Module | Responsibility |
| --- | --- |
| `config/demo_order.yaml` | One sample order: one corn and one carrot to zone A. |
| `config/demo_scene.yaml` | Reproducible O3DE spawn positions; same coordinates as RAI's `1carrot_1corn.yaml`. |
| `config/workstation.yaml` | Camera frames/topics, SKU aliases, reachable area, slots, tolerances, and motion limits. |
| `order.py` | ROS-independent validation, centroid parsing, source selection, and placement checks. |
| `runtime.py` | ROS 2 adapter: waits for interfaces, reads scene identity or perception, calls manipulation, emits events, runs the order state sequence. |
| `cli.py` | Command-line entry, scene preparation, JSON report, exit status. |
| `launch/workstation.launch.py` | O3DE benchmark, MoveIt, RAI perception, and manipulation process. |
| `test/test_order.py` | Fast tests for order rules, position parsing, and safety checks. |

Data path:

```text
order YAML -> validate -> detect SKU -> select source -> grab -> place
                                                    -> re-detect -> verify -> next item
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
depend on the stage. Useful stages are `DETECTING`, `GRASPING`, `PLACING`,
`VERIFYING`, `ITEM_COMPLETED`, `ORDER_COMPLETED`, and `ORDER_FAILED`.

## Run in the existing RAI environment

The base RAI repository is still required. Its downloaded O3DE demo assets,
MoveIt configuration, RAI packages, and model cache are not copied into this
package. Use the existing ROS Jazzy + `rai-jazzy` Conda environment and source
the repository's `install/setup.bash` overlay. On this host, the project root
is `/home/wang/rai_embodied_project`.

```bash
cd /home/wang/rai_embodied_project
source /opt/ros/jazzy/setup.bash
source /home/wang/miniconda3/etc/profile.d/conda.sh
conda activate rai-jazzy
colcon build --packages-select produce_order_workstation --symlink-install
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

`--prepare-scene` spawns the two demo objects into the empty
`RoboticManipulationBenchmark` level before the order starts. Do not run it
against a manually prepared scene unless you intend to add objects. The
default report is at
`~/.local/share/produce_order_workstation/reports/produce-demo-001.json`;
pass `--report /absolute/path.json` to override it. A non-completed order exits
with status 1 and still writes the failure report. The visual demo is not a
substitute for verification: inspect `ORDER_COMPLETED` and the report.

If the model weights are already cached and Hugging Face is unreachable, set
`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` before launching. This only affects
the model loader, not ROS communication.

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
