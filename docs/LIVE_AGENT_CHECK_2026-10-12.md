# 常驻 Agent 的完整仿真检查（2026-10-12）

## 结论与范围

在真实 O3DE 场景、现有视觉模型、百炼 API 和 MoveIt 上，一根胡萝卜订单完成。
这验证的是新常驻 Agent / ExecuteAgent / MCP / ROS 运动链的真实闭环，
不是所有商品、所有数量、所有异常恢复路径的全面验收。

- 订单：`agent-live-20261012-221530-179729`。
- 结果：`COMPLETED`，完成 1/1，error=null。
- 报告：`/home/wang/.local/share/produce_order_workstation/reports/agent-live-20261012-221530-179729.json`。
- 使用公开 SubmitOrder 服务登记并经队列调度，没有绕过感知、库存或运动检查。
- MCP 实际执行：get_object_positions → pick_and_place → finish_order。
- Agent 还提出过一次数量已完成后的重复感知，决策循环拦截，未执行该 MCP 工具。
- ROS 运动阶段：grab、lift、place、retreat。
- 抓取检查：物体 z 从 0.02749 m 到 0.19150 m，抬升约 0.164 m，verified=true。
- ITEM_COMPLETED 和最终物理审核通过，不是只凭模型回答判定成功。

## 启动时遇到的问题

第一次仿真与视觉并发冷启动失败。系统日志记录：
`systemd-oomd killed 10 process(es) in this unit`，服务结果为 `oom-kill`。
这是系统内存保护终止进程，未进入订单执行，不是 MCP/Agent 工具错误。
当时内存 16 GiB，Swap 4 GiB 已有较高占用；不能仅凭可用显存充足排除内存压力。

本次采用分阶段启动：

1. 启动工作站，传入 `enable_perception:=false`，先加载 O3DE、MoveIt、Agent 和 MCP。
2. 确认仿真 clock/相机在线后，在同一 Jazzy/Conda 环境启动
   `ros2 launch rai_bringup openset.launch.py`。
3. 视觉辅助服务命名为 produce-order-perception.service，设置 PartOf 和 BindsTo
   主工作站服务；停止主服务会停止它，避免残留另一套视觉进程。
4. 模型加载和库存扫描完成，工作站状态 READY，才登记测试订单。

没有关闭用户其他应用、清空全局缓存、禁用 OOM 保护或替换视觉模型。
默认启动仍包含视觉；本次分阶段方式是内存压力下的启动绕行，不宣称已根治
所有冷启动内存峰值。不要在默认视觉已启动时再启动第二套 openset。

start_workstation.sh 仅增加了命令行参数转发 `$@`，以支持原本 launch 已有的
enable_perception 参数，默认行为不变。

## 可重复的检查脚本

在已加载当前 Jazzy/工作空间的环境运行：

```bash
python /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/verify_agent_order.py --sku carrot
```

这会发真实订单、调用模型并移动仿真机械臂，不是离线测试。脚本要求工作站
READY、Agent IDLE，不重置桌面、不自动取货；超时不自动重复登记。
结束后仍需查看报告和面板，不能将受理成功视为执行成功。

本次保留 A1 测试订单箱和商品，处于待取货状态。没有自动重置场景或收走箱子。
