# Produce order workstation: mixed inventory + Agent

This ROS 2/O3DE workcell accepts a structured produce order as a **goal**. On
server startup the scene is preloaded with 16 mixed products (four each of
corn, carrot, apple and tomato), plus four visible receiving bins. A Bailian
model chooses perception and grasp tools plus visual candidates; ROS tools own the
physical motion, bin mapping and verification. The products are never generated
one-by-one in response to order lines.

## Modules and data path

| File | Responsibility |
| --- | --- |
| `config/demo_scene.yaml` | Reproducible mixed table inventory and four receiving bins; independent of the order. |
| `config/workstation.yaml` | Stock counts, Agent endpoint/model, fixed SKU-bin mapping, workspace and motion limits. |
| `order.py` | Validate order and bin capacity, parse 3D perception results, determine drop offsets and verify placement. |
| `agent.py` | Bounded OpenAI-compatible tool-calling loop; goal prompt and three tool schemas. |
| `runtime.py` | RGB-D perception adapter, visual-candidate registry, validated pick/place tool, ROS motion service and final audit. |
| `order_server.py` | Persistent ROS Action server, one order at a time, cancellation and JSON reports. |
| `debug_panel.py` | Temporary PyQt debug UI: enter desired counts and watch Agent calls and ROS feedback. |

```text
Order goal (SKU quantities)
   ↓
Server startup: 16 mixed products + A1–A4 receiving bins
   ↓
Agent: get_object_positions(SKU) → choose pick_and_place(detection_id)
       → re-perceive after each pick → finish_order
                           ↓
ROS tool: fresh RGB-D localization → safety checks → grasp → fixed bin → verify
   ↓
Action feedback + /produce_orders/events + JSON report
```

The model may choose *which item* and *when to invoke a tool*. It cannot send
joint values, arbitrary coordinates or a destination bin. Those remain checked
in the deterministic ROS layer. `finish_order` does not blindly trust the
model: the tool checks requested counts and the final position of every picked
entity. The event stream includes `AGENT_TOOL_CALL`, `AGENT_TOOL_RESULT`,
`GRASPING`, `PLACING`, `VERIFYING`, `ITEM_COMPLETED`, `FINAL_AUDIT` and the final
order state.

`get_object_positions` uses the existing RAI perception pipeline: camera RGB →
GroundingDINO detection → SAM segmentation → depth and camera intrinsics →
point cloud → TF to `panda_link0` → 3D centroid. The Agent sees only visual
detection IDs and these estimated coordinates, not simulator entity names or
ground-truth positions. Before motion, the tool re-localizes the chosen visual
candidate. In this simulator demo a hidden entity/TF check rejects known
false-positive classes and verifies final placement; it is a safety/evaluation
oracle, **not an Agent observation or grasp-coordinate fallback**. The detector
can still miss objects or confuse visually similar classes; in that case the
Agent can re-perceive and choose another candidate. Rejected visual locations
are excluded for the rest of the order. If no trustworthy candidate remains,
the order fails safely rather than claiming success. The stock layout remains
fixed and mixed so perception failures are reproducible.

## Start

The base RAI repository, O3DE binary/assets, ROS Jazzy, MoveIt, cached RAI
GroundingDINO/SAM weights and the existing
`rai-jazzy` Conda environment are required. Launch from the **RAI repository
root**, because the launch file resolves demo asset paths relative to it:

```bash
cd /home/wang/rai_embodied_project
source /opt/ros/jazzy/setup.bash
source /home/wang/miniconda3/etc/profile.d/conda.sh
conda activate rai-jazzy
source install/setup.bash
source src/examples/rai-manipulation-demo/ros2_ws/install/setup.bash
unset ALL_PROXY all_proxy  # this host has an unsupported socks:// proxy setting
ros2 launch produce_order_workstation workstation.launch.py
```

The Agent uses the installed `openai` Python package and the Bailian
OpenAI-compatible API. `DASHSCOPE_API_KEY` may be set in the launch environment;
on this workstation it can also be read from the existing mode-600
`~/.config/rai/bailian.env` file. The key is never stored in this repository.
The defaults are `qwen-plus` and the Beijing compatible endpoint. Override
with `PRODUCE_AGENT_MODEL` and `PRODUCE_AGENT_BASE_URL` if needed. Model API
calls may consume paid quota; there is **no scripted fallback** when the key or
API is unavailable.

In a second terminal, source ROS Jazzy and the worktree install overlay, then
open the debug UI with system Python (which has PyQt5):

```bash
cd /home/wang/rai_embodied_project
source /opt/ros/jazzy/setup.bash
source src/examples/rai-manipulation-demo/ros2_ws/install/setup.bash
ros2 run produce_order_workstation debug_panel
```

The launch starts both vision services by default; model initialization can
take around a minute. Only enter quantities; zero means the SKU is not needed. Each SKU has four
pieces of prepared stock and a dedicated bin: corn → A1, carrot → A2,
apple → A3, tomato → A4. Each bin is configured for at most four placements.
The UI therefore permits 0–4 per SKU. The Action goal must have
`prepare_scene=true` (retained as a compatibility flag; it now means the
preloaded workcell is required). After a verified order the server
replenishes the table asynchronously before accepting another order. A failed
order does not auto-reset the uncertain physical state; restart the full launch
before trying again. The report is written under
`~/.local/share/produce_order_workstation/reports/<order_id>.json`.

To check the event stream:

```bash
ros2 topic echo /produce_orders/events
```

To run tests after sourcing the same environment:

```bash
cd /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo
PYTHONPATH=ros2_ws/src/produce_order_workstation:$PYTHONPATH \
  python -m pytest ros2_ws/src/produce_order_workstation/test -q
```

## Boundaries and safety

- The Agent calls a camera-based 3D perception tool. It does not receive raw
  pixels itself; detection and depth projection are performed by RAI tools.
- `pick_and_place` rejects unknown/stale visual IDs, unrequested items,
  unstable coordinates and candidates failing the hidden simulation check.
  The configured SKU→bin mapping cannot be changed by model output.
- A service timeout or failed placement stops the order, because the physical
  state may be unknown. Cancellation is checked before a motion and between
  items, but cannot interrupt a grasp already in flight.
- Scene preparation requires an empty/known simulator world. Restarting only
  the order-server process while keeping O3DE alive can leave orphan entities;
  restart the full launch if old stock remains visible. The server rejects
  orders while initial stock or replenishment is in progress.
- The 16-object layout and per-bin capacity are demo configuration values, not
  an industrial payload claim. Full 16-item order endurance is not yet proven.

Vision-enabled live runs completed corn (`1/1`, report
`visual-agent-20261001-corn-01.json`) and apple (`1/1`, report
`visual-agent-20261001-apple-01.json`). In the apple run, the first visual
candidate failed the hidden class/position safety check; the Agent called
`get_object_positions` again and successfully chose a different candidate.
Inspect `PERCEPTION_RESULT`, `AGENT_TOOL_RESULT` and
`GRASPING.position_source=rgbd_perception` in those reports. This confirms the
RGB-D coordinate path for these two small orders, **not** reliable recognition
or completion of all 16 pieces. Earlier TF-fed Agent runs do not validate the
vision path.
