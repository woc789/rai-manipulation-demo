# 接近运动碰撞修复

## 痛点与证据

此前状态稳定屏障解决的是轨迹起点与实测关节不一致；本次解决的是另一个问题：机械臂能够到达商品上方，却不能沿指定直线完整下降。

原订单 `order-20261008-172032-809040` 最后一件番茄接近阶段的 Cartesian fraction 为 0.8125。第一次改为“先到上方再预检查”的试验 `integration-20261008-191132` 仍在苹果处失败（2/4，fraction=0.875）。保存规划场景与诊断轨迹后，通过 `/check_state_validity` 检查路径后段，明确发现 `panda_link5` 与 `produce_workstation/route_barrier` 碰撞。这是前臂碰撞，不是末端坐标本身不可达。

关闭碰撞检查所得 fraction=1.0 只用于对照诊断，**没有执行这条轨迹**。不能以减少安全检查来“修好”任务。

## 为什么必须联合选择关节姿态

同一个末端位置可能对应多种肘部姿态。以前先让 OMPL 到达悬停位姿，再尝试直线下降：悬停可达并不意味着该关节姿态可以无碰撞下降。

现在先寻找能完成下降的悬停关节配置，再规划怎么从当前位置到达它：

```text
工作站给出抓取目标
  → 运动 Action 服务端选择“转移 + 最后接近”
  → ArmController 求抓取附近的无碰撞 IK
  → 从该姿态反向 Cartesian 到商品上方
  → 从所得悬停关节姿态正向验证下降
  → OMPL 规划当前位置到这组悬停关节值
  → 执行转移、等待实测状态稳定
  → 基于实际当前状态重新规划、检查并执行最后下降
```

## 代码修改与边界

- `robotic_manipulation/src/arm_controller.cpp` 新增 `PlanTransitWithApproach`；头文件增加声明。
- `/compute_ik` 请求 `avoid_collisions=true`，最多六个候选，首个使用当前状态作为种子，其余随机种子。请求超时受限，没有无限循环。
  这是首版修复策略；随后根据用户观察到的大幅绕行，改为八个种子并评分优选，详见 [轨迹优化说明](MOTION_PREFERENCES.md)，当前不再接受第一条可行路径就返回。
- 求 IK 时仅把规划目标下移 1 cm，作为接近余量；**实际抓取指令高度未降低**。这不是对任意扰动都安全的形式化保证。
- 反向和正向 Cartesian 均保持碰撞检查，fraction 必须有限且至少 0.999。
- 使用 `setJointValueTarget` 保留已验证的肘部姿态；若只传末端 pose，后续规划可能重新选择另一组关节解。
- `rai_manipulation_interface_node.cpp` 在存在下降接近的阶段调用新方法。原有状态稳定屏障、执行前起点检查、执行失败后停止规则保留。
- `DiagnoseCartesianFailure` 保存场景、轨迹、目标与 fraction 到 `/tmp/produce_motion_diagnostics/`。无碰撞检查轨迹只供诊断，不能用于执行。

设计参考 [MoveIt Task Constructor 的抓取示例](https://moveit.picknik.ai/main/doc/tutorials/pick_and_place_with_moveit_task_constructor/pick_and_place_with_moveit_task_constructor.html) 中从抓取状态反向构造接近段的思路；本项目没有安装 MTC，也没有宣称已经使用其完整任务求解器，而是使用现有 MoveIt 服务与 MoveGroupInterface 实现这一局部策略。

## 真实验证

- `integration-20261008-191800`：正常视觉、Agent、订单 Action、运动 Action、MoveIt、仿真完整链路，玉米/胡萝卜/苹果/番茄各一件，COMPLETED 4/4；通过公开取货服务释放箱位。
- `integration-20261008-192032`：连续下一单在运动前连续五次未检出玉米，FAILED 0/4。这是仍存在的视觉漏检，不应说成运动修复失败，也不能隐瞒为全链路稳定成功。
- 报告保存在 `~/.local/share/produce_order_workstation/reports/`。测试脚本 `produce_order_workstation/scripts/verify_mixed_order.py` 显式提交真实订单，会调用配置的模型 API；不绕过视觉、不会自行恢复或取货。
- Python 后端 87 项通过；系统 PyQt5 的界面测试 9 项通过；原有关节状态保护四项 GTest 通过。测试通过不意味着所有随机物品布局都已覆盖。

部署时还发现临时 hotfix 服务的 `PartOf` 随整台重启，同时启动了两个运动接口；此轮启动日志出现 MoveIt `double free or corruption` 并无法完成服务初始化。已停止并卸载临时瞬态服务，然后重新启动正式 launch；不能让临时替换进程与正式节点长期并存。二次启动只有一个运动接口，初始化运动成功，运动 Action 就绪。这里只记录共同出现的证据，不把内存错误未经调试地归结为已证明的唯一原因。
