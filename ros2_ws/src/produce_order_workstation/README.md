# Produce order workstation: mixed inventory + Agent

This ROS 2/O3DE workcell accepts a structured produce order as a **goal**. On
server startup the scene is preloaded with 16 mixed products (four each of
corn, carrot, apple and tomato), **no receiving bins**, and a side-laid wooden
return crate as the physical obstacle. Four locations remain available for
order boxes, but a box appears only after its order is accepted. A Bailian
model chooses perception and grasp tools plus visual candidates; ROS tools own the
physical motion, bin mapping and verification. The products are never generated
one-by-one in response to order lines.

## Modules and data path

| File | Responsibility |
| --- | --- |
| `config/demo_scene.yaml` | Reproducible mixed inventory, four bins and a physical route barrier; independent of the order. |
| `config/workstation.yaml` | Operation mode, initial stock, Agent endpoint/model, legacy SKU-bin mapping and motion limits. |
| `fulfillment.py` | In-memory bin ownership, allocation, waiting-for-pickup and collection lifecycle. |
| `order.py` | Validate order and bin capacity, parse 3D perception results, determine drop offsets and verify placement. |
| `agent.py` | Bounded model/tool loop; discovers tool schemas and calls tools through MCP. |
| `agent_service.py` | Launch-started independent ROS Agent runtime, serial task admission, model client reuse and per-task decision context. |
| `agent_dispatch.py` | ExecuteAgent Action client: readiness, feedback, cancellation and results; no model logic. |
| `mcp_client.py` | Official MCP ClientSession: initialize, discover tools, invoke tools and read order context over loopback HTTP. |
| `mcp_server.py` | Order-scoped MCP facade for three workstation tools and one read-only resource; token guard, serial execution and safe shutdown. |
| `runtime.py` | RGB-D perception adapter, visual-candidate registry, validated pick/place tool, ROS motion Action client and final audit. |
| `order_server.py` | Persistent ROS Action server, one order at a time, cancellation and JSON reports. |
| `debug_panel.py` | Temporary PyQt debug UI: enter counts, watch Agent calls and ROS feedback, retry or recover the workcell. |
| `planning_scene.py` | Synchronize table, bin and verified barrier proxies; attach/detach the audited carried-item sphere. |
| `PLANNING_SCENE_PLAN.md` | Phased design, validation gates, geometry assumptions and rollback risks. |
| `produce_order_interfaces/action/ExecuteMotion.action` | Typed single-motion goal, stage feedback and failure result. |
| `robotic_manipulation` | MoveIt/hand Action server; retains the legacy `/manipulator_move_to` service. |

```text
Order goal (SKU quantities)
   ↓
Server startup: 16 mixed products + side-laid return crate (no order boxes)
   ↓
Accept order: allocate A1–A4 location → spawn this order's box → sync MoveIt
   ↓
Agent: get_object_positions(SKU) → choose pick_and_place(detection_id)
       → re-perceive after each pick → finish_order
                           ↓
MCP Client → Streamable HTTP → order-bound MCP Server
                           ↓
ROS tool: RGB-D re-localization → safety checks → grasp → allocated bin → verify
   ↓
Action feedback + /produce_orders/events + JSON report
   ↓
WAITING_PICKUP → human confirmation → remove this order's goods AND box → free location
```

## MCP tool boundary (2026-10-11)

The default Agent path now uses real MCP Streamable HTTP, not direct Python tool
dispatch. Bailian still selects tools via its model API. The host starts a local,
order-bound MCP server, the client discovers schemas with `tools/list`, and
`tools/call` invokes workstation capabilities. No public port or API key is exposed.
ROS launch now starts an independent `agent_server` process and `order_server`'s
persistent MCP gateway. `Workstation.run` binds an admitted order and sends an
ExecuteAgent Action; it neither constructs an Agent nor accesses the model.
The Agent service waits idle, reuses its model API client and creates isolated
decision/context and MCP client sessions per task. Existing order/motion Actions,
inventory checks and physical audits remain. See
[AGENT_RUNTIME.md](../../../docs/AGENT_RUNTIME.md) for current operation and
[MCP_INTEGRATION.md](../../../docs/MCP_INTEGRATION.md) for earlier migration history.

## Fulfillment phase 0–1 (2026-10-08)

Default `operation_mode: fulfillment` assigns **one mixed order to one free location**
and spawns that order's box on acceptance. Explicit pickup removes both its goods
and its box; it does not leave four empty boxes on the table. See
`DYNAMIC_BOX_SCENE.md` for this follow-up correction and the realistic crate geometry.
`classification` preserves the original corn/A1, carrot/A2, apple/A3, tomato/A4
placement mode for regressions. Both modes now require explicit pickup and never
automatically replenish after success. See `FULFILLMENT_PHASE01.md` for the
baseline, state/interface design, implementation details and acceptance checklist.

The current small-bin configuration permits **four items total per mixed order**,
not four per SKU in fulfillment mode. This is a bounded simulation configuration,
not a measured packing-capacity claim. Distinct release offsets do not constitute
collision-aware 3D packing. Larger orders need larger bins and a validated placement
strategy, not just a larger UI limit.

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

The carrot prefab has a repeatable RGB-D centroid bias of roughly +2–3 cm in
X in this fixed camera/workcell. `motion.grasp_correction_m.carrot` applies a
measured offset to the visual point before motion; it is a fixed calibration,
not a simulator-pose replacement. After closing the gripper the arm lifts the
object while staying closed. A simulation-only `GRASP_CHECK` verifies that the
selected object's Z actually increased before the arm transports it to a bin.
If the lift fails, the order stops before placing and requires scene recovery.
The motion Action's `success` flag alone does not prove a successful grasp.
On 2026-10-02, live visual-Agent orders completed one carrot (`1/1`, report
`repair-carrot-lift-20261002-01.json`) and then three carrots into A2 (`3/3`,
report `repair-carrot-lift-20261002-03.json`); every `GRASP_CHECK` recorded a
real lift. This validates the repeated carrot failure path, not the full
ten-item mixed order.

The order Action and motion Action have different responsibilities. The order
Action owns an entire customer order; `/produce_orders/execute_motion` executes
one bounded arm/gripper motion. `Workstation` sends explicit `grab`, `lift`,
`place` and `retreat` goals, so it can verify an actual lift *before* transporting
the item. Order motion goals report `INITIAL_GRIPPER`, `PLANNING_TRANSIT`,
`EXECUTING_TRANSIT`, optional local Cartesian stages, `FINAL_GRIPPER` and
`DONE` feedback, which is forwarded as
`MOTION_STAGE` order events. The C++ server refuses concurrent motion goals,
legacy service calls and resets. Cancellation is honored at stage boundaries;
it does not interrupt a blocking MoveIt trajectory mid-flight. A canceled or
timed-out motion leaves the workcell in `RECOVERY_REQUIRED` because the physical
state may be uncertain. The legacy service remains available to old RAI tools.

The C++ controller rejects incomplete Cartesian plans (fraction below 0.999),
limits gripper retries, and bounds gripper Action waits. This does not impose a
hard deadline on MoveIt's own synchronous `move()`/`execute()` calls; that is a
remaining controller-level limitation.

For `grab` and `place`, the C++ controller splits one motion
Action into a clearance move, a collision-aware MoveIt/OMPL transit plan and
a short Cartesian final approach when needed. `lift` and `retreat` are explicit
local Cartesian motions; an XY displacement of 2 cm or more is rejected before
operating the gripper. `grab` uses open -> closed, `lift` closed -> closed,
`place` closed -> open, and `retreat` open -> open gripper commands. The server
rejects unknown phases and inconsistent gripper flags at goal acceptance.
The old `hold`/`release` Action phase names are no longer supported. The legacy service retains its old
Cartesian waypoint route. After a verified physical lift, the workstation
transforms the selected entity's centre into `panda_hand` and attaches a
conservative sphere to MoveIt's robot state. It removes that sphere after
release; scene recovery clears stale attachments. This simulated pose is used
only for collision safety, never as an Agent observation or grasp target.

Historical obstacle experiment: the original side-lying `toy_box` divided the route but blocked the oblique
RGB-D camera's view of some products. An earlier route marker instead stacked
four existing 5 cm rigid-body `yellow_cube` prefabs at (0.37, 0). They are
spawned sequentially to let each settle. Startup reads all four physical
poses and rejects a missing, displaced or tipped cube before adding one
conservative 0.06 × 0.06 × 0.21 m MoveIt proxy. Two products were moved
from the marker's camera projection into nearby open positions; inventory
counts and SKU-bin mapping are unchanged. A plan-only crossing probe with a
simulated carried sphere found the direct Cartesian route 100% complete
without the marker and about 73% complete with it, while the OMPL free-space
plan remained successful. This demonstrates a planning effect, not a loaded
operator-order acceptance result. The unchanged Grounding DINO model may
still miss or misclassify objects that are visible to a human.

The Agent processes requested SKUs in order and must finish the full quantity
of one SKU before switching. The model still selects a fresh RGB-D detection
for every pick; the service owns the count and rejects premature completion.
Unexpected zero/multiple tool calls are returned as retryable feedback without
moving the arm. `ITEM_COMPLETED` feedback includes the per-SKU count and all
remaining quantities, and reports retain the submitted order lines.
Five consecutive empty visual results for the same needed SKU stop the order
with `PERCEPTION_EXHAUSTED` and an explicit remaining count; unrelated model
responses do not silently reset this counter. A successful detection or pick
resets it.

Each perception call invalidates every earlier visual ID and generates IDs
with a new generation number. If the Agent attempts a pick without fresh
perception, or selects a stale detection, the tool returns a retryable error
without moving the arm. The model can then call `get_object_positions` and
continue within its bounded tool-call budget. Genuine motion failures still
stop the order because the physical state may be uncertain.

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

On the 16 GiB desktop, the preferred GUI launch is a separate user service so
an out-of-memory kill of the simulator does not terminate the Codex desktop
process. The start scripts clear inherited ROS Kilted paths before loading
Jazzy; the simulator script also bounds allocator arenas and BLAS threads.
Run these from a graphical login (adjust `DISPLAY` and `XAUTHORITY` if needed):

```bash
systemd-run --user --unit=produce-order-workstation --collect \
  --property=WorkingDirectory=/home/wang/rai_embodied_project \
  --setenv=DISPLAY=:1 --setenv=XAUTHORITY=/run/user/1000/gdm/Xauthority \
  /bin/bash /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/start_workstation.sh
systemd-run --user --unit=produce-order-panel --collect \
  --property=WorkingDirectory=/home/wang/rai_embodied_project \
  --setenv=DISPLAY=:1 --setenv=XAUTHORITY=/run/user/1000/gdm/Xauthority \
  /bin/bash /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/start_debug_panel.sh
```

Check `systemctl --user show produce-order-workstation.service -p ActiveState
-p MemoryCurrent -p MemoryPeak` and `journalctl --user -u
produce-order-workstation.service -f` if the machine becomes slow. This
isolates failures and lowers allocator overhead; it does not guarantee that
every possible high-memory order can complete. Stop the units only when no
order is running.

The Agent uses the installed `openai` Python package and the Bailian
OpenAI-compatible API. `DASHSCOPE_API_KEY` may be set in the launch environment;
on this workstation it can also be read from the existing mode-600
`~/.config/rai/bailian.env` file. The key is never stored in this repository.
The defaults are `qwen3.7-flash-2026-07-15` and the Beijing compatible endpoint. The
model supports Function Calling, and a live tool-call smoke test succeeded
on 2026-10-02 with the existing private API key. The model may be covered by
the account's time-limited free quota only while quota remains and its
"free-tier-only" switch is enabled; this is not a permanent free API. Override
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
take around a minute. Only enter quantities; zero means the SKU is not needed.
Each SKU initially has four pieces. In fulfillment mode the whole order uses
one server-allocated empty bin, with at most four items total. The UI permits
0–4 per SKU but rejects a combined total over four. The Action goal must have
`prepare_scene=true` (retained as a compatibility flag; it now means the
preloaded workcell is required). After a verified order the server retains the
goods and bin ownership as `WAITING_PICKUP`; the table is not replenished. A failed
order with attempted arm motion requires explicit scene recovery before retry.
The report is written under
`~/.local/share/produce_order_workstation/reports/<order_id>.json`.

The panel now displays the server's workcell state (`INITIALIZING`, `READY`,
`RUNNING`, `RECOVERING`, `RECOVERY_REQUIRED`) and each bin's ownership. Submit is enabled
only when `READY` and a bin is available. **重新提交本单** keeps the quantities but generates a fresh order
ID. **补货 / 恢复（重建场景）** calls `/produce_orders/recover`, which clears and
recreates the simulator stock/bins; it does not restart the O3DE executable.
It requires confirmation and is disabled while an order/preparation is running
or a completed order still awaits pickup. A failed or
cancelled order with no attempted arm motion returns to `READY` automatically;
after any attempted motion the server requires explicit scene recovery. A
completed order never replenishes automatically. Select a waiting order and use
**确认取货并释放箱位** (`/produce_orders/confirm_pickup`, `ConfirmPickup.srv`). This
simulates human collection and replacement with an empty bin: only that order's
verified goods and physical order box are deleted; table stock stays unchanged.
Cleanup failure retains ownership for retry. A separate `<order_id>.pickup.json`
receipt records successful collection without rewriting the execution report.
If O3DE or its ROS services have
crashed, restart the full launch from the terminal; scene recovery cannot
restart a dead simulator process.

The screenshot from 2026-10-01 corresponded to a Bailian `Arrearage` API
response. That happened before any arm motion, so the new server stays ready
for another order, but retrying cannot succeed until the API account issue is
resolved. The panel shows this reason directly instead of a generic inventory
message; the original error remains in the JSON report.

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
  Server allocation (or the legacy SKU→bin mapping) cannot be changed by model output.
- An Action timeout or failed placement stops the order, because the physical
  state may be unknown. Cancellation is forwarded to the motion Action and
  honored at the next motion-stage boundary, not in the middle of a MoveIt
  trajectory.
- Scene preparation requires an empty/known simulator world. Restarting only
  the order-server process while keeping O3DE alive can leave orphan entities;
  restart the full launch if old stock remains visible. The server rejects
  orders while initial stock or explicit scene reset is in progress. Bin ownership
  is currently in memory; restarting the launch is not a durable business recovery.
- The 16-object layout and per-bin capacity are demo configuration values, not
  an industrial payload claim. Continuous inventory, reservations, a queue,
  behavior trees and restart reconciliation are subsequent phases, not implemented here.

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
