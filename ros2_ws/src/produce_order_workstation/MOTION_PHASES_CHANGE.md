# Explicit motion phases (2026-10-07)

## Problem and scope

Two `release` calls represented different operations: carrying an item to a
bin and retracting after release. The C++ server inferred intent from XY
distance, while Python assigned closed -> open gripper commands to both.
Consequently retract unnecessarily closed the already-open gripper.

## Changes

- `Workstation.move(point, step=..., target_z=...)`: use motion intent, not
  the ambiguous `grip` name. Four explicit phases and defaults:
  `grab` (open -> closed, visual Z + gripper offset),
  `lift` (closed -> closed, configured lift height),
  `place` (closed -> open, configured release height),
  `retreat` (open -> open, configured exit height).
- `pick_and_place()` sends these four phases in order; lift audit,
  collision attachment/detachment and order accounting are unchanged.
- `RunMove()` selects Cartesian motion only for explicit lift/retreat;
  grab/place retain collision-aware free-space transit plus local approach.
  Local phases reject XY changes >= 2 cm before any gripper command.
- Action goal acceptance rejects unknown phases and inconsistent gripper
  booleans. Diagnostic `smoke` remains open -> open. The legacy Service
  keeps its existing `legacy` route and gripper flag behavior.
- The Action message fields are unchanged; comments document phase semantics.
  Old `hold`/`release` Action clients must migrate. Restart both Python and
  C++ processes together after building to avoid mixed protocol versions.
- Tests check all phase names, gripper states, default heights, rejected old
  names, the four-call grasp sequence and existing cancellation/failure paths.

## Knowledge and follow-up

This fixes an application-level ROS Action contract, separating task intent
from gripper state. The obstacle integration itself combines MoveIt Planning
Scene services, collision primitives, TF, attached bodies, motion Action
feedback, OMPL planning and joint-state/FK diagnostics. Agent tool names are
unchanged: perception, validated pick/place and final order audit.

Recommended next increment: observable, typed single-item manipulation Action
with a deterministic execution state machine and structured failure codes.
Move orchestration out of the Agent-facing adapter while preserving the
Agent's choice of tools/items. Add task-level feedback, bounded recovery and
fresh synchronized perception evidence. Do not allow the language model to
override collision checks or invent coordinates. MTC/behavior-tree adoption
should follow a measured need, not merely add another framework.

## Verification

- 48 workstation tests passed (7 additional phase-contract cases).
- `produce_order_interfaces`, `produce_order_workstation` and
  `robotic_manipulation` built successfully. C++ reports the existing obsolete
  MoveIt `.h` include advisory; no compilation errors.
- Restarted Python and C++ together; workcell returned READY.
- Live Action server rejected old `release` and a `retreat` requesting
  closed -> open flags. Both probes were rejected before motion.
- An empty-hand `retreat`, using current FK pose with Z increased 2 cm and
  open -> open flags, returned SUCCEEDED. No order or product grasp was sent.
- Loaded full-order regression remains for the operator's own order.
