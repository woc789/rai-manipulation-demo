# 运动段衔接：轨迹起点状态不一致修复

日期：2026-10-08；基线 `bc28ea4`。

## 问题与证据

订单 `order-20261008-171755-496472` 完成玉米、胡萝卜后，Agent 用视觉工具获取苹果候选并选中 `visual_apple_5_1`。故障发生在该苹果的抓取运动，夹爪尚未闭合抓取。

17:20:32 的实际后台日志：

1. `32.613300`：自由空间转移执行成功。
2. `32.614811`：收到最后接近的 Cartesian 规划请求，相隔约 1.5 ms。
3. `32.618035`：Cartesian 轨迹生成完整，fraction=1.0。
4. `32.619186`：执行前校验拒绝：轨迹起点与当前 `panda_joint2` 偏差超过 0.01 rad。

确认问题是运动段衔接时的状态一致性，而非本次视觉未找到目标、OMPL 未找到避障路线或 Cartesian 路径覆盖率不足。旧代码没有等待实测关节稳定，Cartesian 请求使用空 diff，由 move_group 的异步场景状态决定起点；轨迹执行校验又读取当前反馈。相邻操作过快，两个状态快照可能不同。

日志没有记录两份实际关节值，因此不能进一步断言全部偏差来自物理残余运动还是状态监视器更新延迟。修复覆盖两者：先等待新鲜稳定状态，再显式传入实测起点并做执行前核对。没有声称用这条日志定位了 O3DE 内部控制器的具体缺陷。

核对官方 Jazzy 源码：[`computeCartesianPath` 将 considered_start_state_ 直接赋给请求](https://github.com/moveit/moveit2/blob/jazzy/moveit_ros/planning_interface/move_group_interface/src/move_group_interface.cpp)；`setStartStateToCurrentState` 设置的是空 diff，不是在客户端锁定一个实测快照。

## 修改模块

### 1. 实测状态稳定保护

`robotic_manipulation/include/robotic_manipulation/joint_state_guard.h`

- `MaxError`：按关节顺序求最大绝对差；空数组、长度不符、NaN/Inf 拒绝。
- `StabilityWindow::Observe`：要求时间前进，至少三个新状态，在不少于 200 ms 的窗口中相对锚点变化不超过 0.001 rad。
- 比较窗口锚点而非仅比较相邻采样，避免慢速漂移被误认为停止；重复时间戳不能冒充多个新状态。

### 2. ArmController：运动前后建立状态屏障

`robotic_manipulation/src/arm_controller.cpp` 与 `include/.../arm_controller.h`

- `WaitForStableState`：每次调用 `getCurrentState(0.4)` 等待新反馈；结合稳定窗口判断，最多约三秒（最后一次读取可能额外耗时 0.4 秒），失败即停止，不用无限等待。
- `SetMeasuredStartState`：向 MoveIt 请求传入明确的七个机械臂关节值。RobotState 仍设 `is_diff=true`，所以保留场景里的附着碰撞体；不能为了固定关节起点把抓住商品的碰撞模型清掉。
- `CheckTrajectoryStart`：按轨迹 joint_names 对齐实测值，记录最大关节偏差，超过 0.005 rad 则拒绝发送。比 MoveIt 原有 0.01 rad 检查更严格，未修改原阈值。
- `MoveThroughWaypoints`：稳定实测起点 → Cartesian 规划 → 恢复默认起点设置 → 核对起点 → 执行 → 再等待稳定。执行前起点漂移可以重新规划，最多三次；一旦 execute 已失败，立即返回，不盲目重发可能已部分执行的轨迹。
- `PlanFreeSpaceToPose` / `ExecutePlan`：自由规划也使用稳定实测起点，执行前核对，执行成功后等待稳定。它与 Cartesian 阶段共享保护，避免只修一处。
- `GetLastMotionError`：保留状态不可用、规划不完整、起点变化或执行错误码等具体原因。

### 3. Action 错误传播

`robotic_manipulation/src/rai_manipulation_interface_node.cpp`

最后接近失败时，将 ArmController 的具体原因加入 Action result，而不是统一丢失原因后只返回 `Local Cartesian approach failed`。工作站仍负责整单统筹与失败锁定；Agent、订单协议、碰撞检查、夹爪工具均不改。

### 4. 验证工具与测试

- `robotic_manipulation/test/test_joint_state_guard.cpp`：四项 GTest 验证新鲜稳定、过期重复状态、累计漂移、异常数据。
- `produce_order_workstation/scripts/probe_motion_handoff.py`：默认只读规划；加 `--execute` 才进行三次空手 smoke Action。拒绝正在执行的订单、已附着商品、无同步状态及不安全起始位置；先检查 Cartesian 路径无碰撞。目标保持当前 XY，Z 为 0.28/0.27/0.28 m；不提交订单、不调用模型、不抓商品、不恢复桌面。测试结束会停在最后一个诊断目标，不自动回到测试前姿态。

## 验证结果与运行状态

- `robotic_manipulation` 编译成功；四项 GTest 通过。
- 现有订单相关 Python 单测 76 通过，Conda 中缺少 PyQt5 的 UI 测试单独跳过。
- 只读采样：空闲关节在约 1.44 秒仿真时间中收到 37 条反馈，变化为零；说明反馈本身并非一直坏掉。
- 实际空手测试：预规划 fraction=1.0；三次 smoke Action 全部 success=true，确实包含转移与最后接近阶段。
- 修复后日志记录的部分起点误差为 0.000104 和 0.000123 rad；未出现此前起点校验拒绝。
- 没有执行真实完整订单，不能把空手测试等同于全部商品抓放已验证。

本次仅替换了旧机械臂接口进程，仿真、MoveIt、视觉与订单服务均保留。新接口暂由 `produce-manipulation-hotfix.service` 托管，并设置 `PartOf=produce-order-workstation.service`：停止/重启整个工作站时随之停止，下一次正常启动由 launch 启动已安装的新二进制，避免残留重复 Action 服务端。当前只有一个运动接口进程。

上一单的 A1 仍是 RECOVERY_REQUIRED，不把失败单强行标为成功，不清理已装商品。用户恢复工作台后自行填写下一单验证。
