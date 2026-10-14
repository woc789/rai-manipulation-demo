# Agent 规划受约束行为树：接口与安全设计

## 目标与职责

一单进入执行后，Agent 根据订单剩余量、箱位和视觉库存摘要提出**商品处理顺序**及**逐件视觉重试上限**。模型不输出 BT XML、抓取坐标、关节命令或成功判定。工作站校验提案，并通过固定编译器生成 BT；BT 按顺序调用已有的感知选择、抓放验证和最终审计能力。MoveIt 仍规划运动轨迹。

这不是通用的自然语言转任意行为树，而是可审计的领域专用策略编译器。这样面试时可以准确说明 Agent 的决策权和机器安全边界。

## 输入合同

`planning_context()` 构造版本化快照：订单号、区域、请求量、已完成量、剩余量、分配箱、库存观测摘要、可用能力与重试上限。库存计数只表示近期观测，不是仿真真值；不向模型提供初始物体坐标。快照以规范 JSON 计算 SHA-256 摘要，计划必须引用相同摘要。Agent 通过订单范围 MCP 资源核对订单身份和已完成量，规划快照通过 ROS `ExecuteAgent` Action 传入。

模型仅能调用 `propose_fulfillment_plan`，输出：

```json
{
  "version": 1,
  "context_digest": "<sha256>",
  "items": [
    {"sku": "apple", "visual_attempts": 2},
    {"sku": "corn", "visual_attempts": 5}
  ]
}
```

每个 `items` 元素代表**一件**。对每个 SKU，元素数量必须严格等于订单剩余数量；重试上限必须在 `1..config.agent.max_consecutive_empty_perceptions` 内。未知字段、额外商品、漏项、过期摘要或非法预算一律拒绝，规划失败时不会开始运动，也不会默默回退成旧脚本。

## 运行链路

```text
OrderActionServer 接纳并绑定箱位
  -> Workstation.run 建立订单范围 MCP 会话
  -> ExecutionBridge 汇总规划快照
  -> AgentDispatcher / ExecuteAgent(mode=plan)
  -> ProduceAgent 调用 propose_fulfillment_plan
  -> validate_plan 检查订单守恒与重试边界
  -> compile_tree 生成固定节点集合的 AgentPlan XML
  -> RunFulfillment(plan_xml) 运行 C++ BT
  -> FulfillmentStep(select, target_sku) -> MCP 视觉定位 -> Agent 选候选 ID
  -> FulfillmentStep(pick, target_sku) -> Workstation 抓放、验证、记账
  -> 下一件；全部验证后 FulfillmentStep(audit)
```

`target_sku` 同时经过 BT 节点、ROS Service 和工作站执行桥。桥接层检查它与已批准的逐件计划一致；`pick` 还必须使用本会话刚选出的候选 ID。每一次成功抓放才推进计划游标。视觉失败按该件的预算重试；动作结果不确定时终止并交由既有现场核对流程，不自动重放动作。恢复后的订单只规划**剩余**件数。

## 当前边界与后续扩展

当前实现是**每次订单执行入口规划一次**，并不是 Agent 在每个 BT tick 时改写树。失败分支仍由 BT 和工作站确定性处理；运动失败、实体状态不明、取消等不会触发自动再规划。将来若要做反馈驱动再规划，必须只在安全静止边界生成新快照，撤销旧计划并记录版本，禁止重做状态不明的抓放。`RunFulfillment` 保留空 `plan_xml` 的旧集成测试路径；生产 `ExecutionBridge` 总是提供已验证计划。

## 验证方式

单元测试覆盖计划顺序改变执行顺序、非法计划拒绝、过期上下文拒绝、ROS 步骤越界拒绝和已验证抓放只推进一次；ROS 集成测试覆盖 Action/Service 通信。实际仿真订单仍需在启动完整感知/运动场景后人工确认，单元与 ROS 空场景测试不能证明真实抓取成功。
