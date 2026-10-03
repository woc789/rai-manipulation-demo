# MoveIt Planning Scene integration plan

## Objective and baseline

The workstation completed a seven-item mixed order (`debug-20261003-155642-354882`,
7/7) before this change. Preserve its order Action, visual Agent tools, motion
Action and physical lift/bin audits. The objective is to make MoveIt aware of
the fixed workcell geometry that O3DE already renders, without allowing a
miscalibrated collision proxy to silently break picking.

## Ownership and data flow

`demo_scene.yaml` defines which bins O3DE spawns. `workstation.yaml` defines
their slot mapping, workspace and deliberately conservative collision sizes.
After O3DE has spawned and settled the scene, `Workstation.prepare_scene()`
checks each bin's live O3DE pose, builds five MoveIt collision objects, calls
`/apply_planning_scene`, then verifies them via `/get_planning_scene` before
the order server can publish `READY`. The `produce_workstation/` ID prefix
marks owned geometry; synchronization replaces those objects and removes only
obsolete IDs with that prefix. Other packages' planning objects are untouched.

The launch file defines an identity `world` → `panda_link0` transform. Bin
poses obtained from O3DE and the source workspace therefore use the
`panda_link0` planning frame. This alignment must be revisited if the launch
TF changes. The bin proxies have a floor and four short walls, leaving the
opening clear. The table is a low slab below the stock and grasp poses. The
proxies are not precise meshes of O3DE prefabs; their heights are explicit
calibration values that must be checked against the live scene.

## Delivery gates

1. **Fixed scene:** add the table and A1–A4 proxies, validate all finite and
   positive dimensions, verify service acknowledgment and readback. On failure,
   the workcell must not enter `READY`. Replenish and recovery must re-sync the
   same geometry. Unit tests cover dimensions, missing bins, owned-object
   cleanup and failed readback. An idle live run must show all five IDs in
   MoveIt's planning scene without causing arm motion.
2. **Trajectory compatibility:** ask the operator to submit a normal order;
   compare Action feedback, MoveIt planning errors, physical lift and final
   bin audit against the 7/7 baseline. If the new geometry blocks a known-good
   pose, measure and correct the proxy rather than disabling collision checks.
3. **Dynamic carried stock (separate change after gate 2):** after a verified
   physical lift, derive a collision proxy from the *fresh visual pose* and
   attach it to the correct end-effector link in MoveIt; detach it on release
   and reconcile failures/recovery. Do not expose O3DE truth as Agent input or
   pretend an attached planning object proves a real grasp. This stage needs
   TF, link and geometry calibration and its own repeated-order acceptance run.
4. **Multi-stage planning (later):** evaluate MoveIt Task Constructor after the
   fixed/dynamic scene is reliable. MTC plans approach, grasp, lift and place
   stages; it does not replace order accounting or real lift/bin verification.

## Operational checks and risks

- `ros2 service call /get_planning_scene moveit_msgs/srv/GetPlanningScene
  "{components: {components: 16}}"` should list the five managed IDs after
  startup. `16` requests world-object geometry.
- Startup and scene recovery can now fail closed if MoveIt is unavailable.
- An O3DE scene reset while an order is running is unsupported; the existing
  busy guard prevents the ordinary recovery path during an order.
- The current geometry is intentionally low and approximate. Never add all 16
  moving stock objects as static obstacles; stale poses can block valid grasps.
- No order is auto-submitted during implementation. Only the user chooses the
  order and authorizes physical execution. A clean git commit records changes
  after tests and idle-scene verification.

## Acceptance record (2026-10-03)

- Package build and all 37 workstation tests passed. The idle workstation
  published `READY`, and `/get_planning_scene` returned the table plus A1–A4.
  The current robot state passed MoveIt's collision-validity check.
- The operator's first seven-item order stopped before any arm motion because
  corn detection returned no masks on five attempts. Planning Scene does not
  affect the RGB-D image or the detection service, so this is not evidence of
  a collision-planning regression.
- The next operator order detected corn and executed the arm's approach and
  Cartesian motion (MoveIt reported a 100% Cartesian path). It then failed
  while closing the hand: `panda_hand_controller` exceeded MoveIt's 0.5 s
  execution allowance on three attempts. This is a gripper-controller timeout,
  not a rejected arm path. The operator recovered the workcell afterward.
- A third operator-selected order (`debug-20261003-163515-898742`) completed
  7/7: one corn to A1, two carrots to A2, one apple to A3 and three tomatoes
  to A4. Each item passed the physical lift and destination-bin checks. This
  passes gate 2 for the fixed collision proxies, including repeated placement
  into A2 and A4. The earlier gripper timeout did not recur; investigate the
  controller response separately if it becomes repeatable.
- Gates 3 and 4 are deliberately later changes: attached-object geometry
  needs end-effector calibration and failure reconciliation, while MTC needs
  fixed/dynamic scene reliability. Neither is silently bundled into this
  fixed-scene delivery.
