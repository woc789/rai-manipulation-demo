# 第 2、3 步可靠性修复与学习入口

日期：2026-10-09。对应原方案第 2 步持续库存、第 3 步多订单调度，不推进第 4～6 步。

## 一、这次修复的前后区别

### 库存：历史记录不等于当前可承诺数量

旧行为：同一类别两次观测的位置相差超过 3.5 cm，就生成新 ID；旧候选在 60 s TTL 内仍为 AVAILABLE。
在“同一物品移动 6 cm”的输入下，账本会允许预留两件，尽管最新观测只有一件。

新行为：`InventoryLedger.observe(..., complete_scan=True)` 在同 SKU、同坐标系内关联一轮候选。
匹配成功的记录保留 ID 和所有者；未匹配的旧记录保留历史但转为 UNKNOWN，不参与可用预留。
空观测也保留记录并标为不确定，不能因此宣称实物已经没有了。

如果未匹配的旧记录仍有订单所有者，新出现的无主候选可能正是该订单预留物品移动后的身份，
因此这一轮先标为 UNKNOWN，不交给其他订单；重复扫描也不能绕开这个保护。
旧位置重新被匹配后可恢复原归属；取消尚未开始的原订单后，可在新一轮视觉扫描重新确认可用候选。
这是保守的关联策略，不是视觉跟踪模型，也不能保证真实物品身份。宁可等待复查，不虚增承诺。

`complete_scan=False` 表示仅补充一个已定位目标，不代表扫描了全部候选。
`runtime.py` 中单目标兜底登记使用此模式，避免将未包含在单目标更新中的其他库存标成未知。
正常库存扫描和 Agent 的类别候选扫描使用默认完整扫描模式。

### 调度：有界等待，但不把超时当作“已经停机”

旧行为：发送执行 Goal 后，等待接收 Future 和结果 Future，没有应用层等待时限。

新行为：独立 ROS Timer 每秒检查以下可配置时限：

| 配置 | 默认值 | 监测什么 |
| --- | --- | --- |
| scheduler.accept_timeout_s | 15 s | 从发出 Goal 到接收响应 |
| scheduler.feedback_timeout_s | 180 s | 已接收执行的反馈停滞 |
| scheduler.result_timeout_s | 900 s | 从发出 Goal 起的总执行等待 |

使用 `time.monotonic()`，不依赖可能暂停或跳变的仿真时间。
心跳读取和超时状态转换在同一互斥锁内完成，避免刚收到反馈却用旧心跳判超时。

超时或执行通信异常时，订单变为 EXECUTION_UNCERTAIN，工作台变为 RECOVERY_REQUIRED。
保留库存所有权、箱位及当前执行关联，禁止后续调度；如果已经拿到 GoalHandle，发送取消请求。
取消仍由工作站在安全检查点处理，不是急停，不承诺马上停止，也不自动重新发起抓取。

迟到的接收响应：仍为不确定状态，并请求取消，不恢复 RUNNING。
迟到的结果：按真实业务结果记录完成数量/待取货/暂停状态，但仍要求核对现场，不自动开始下一单。
明确拒绝的请求：在没有箱位分配时释放未执行预留；超时保护状态仍等待人工恢复。
旧订单的接收或结果回调不能污染另一个当前订单。

如果通信一直不恢复，系统明确停留在“待核对”，不会偷偷释放库存或强行重置。
需先确认执行端已停止并核对夹爪、商品、箱子，再处理停机/重启。
目前还没有第 6 步的跨进程恢复能力，不能将重启解释为自动无损恢复。

## 二、学习时先看这一条主链

```text
你填写订单
  → 界面 ROS 工作线程调用 SubmitOrder Service
  → OrderActionServer 校验并交给 OrderQueue 登记
  → 空闲时 Workstation 调视觉工具扫描，更新 InventoryLedger
  → OrderQueue 检查箱位并要求 InventoryLedger 整单预留
  → 调度器 ActionClient 发送 ExecuteOrder Goal
  → 同节点 ActionServer 再校验、分配箱位
  → Workstation 调 Agent；Agent 调定位、抓取放置、结束工具
  → 原运动 Action / MoveIt 执行每段运动
  → 单件退出、装箱验证后提交库存 PLACED 和订单数量
  → 若有资源满足的更高优先级订单，在此安全边界暂停当前单
  → ExecuteOrder Result 回到调度器，继续急单或恢复原单
  → 全单完成保留箱子待取货；人工确认取货才释放箱位
```

## 三、模块对象、上下游与输入输出

以下路径相对 `ros2_ws/src/produce_order_workstation/produce_order_workstation/`。

| 模块与对象 | 上游输入 | 主要工作 | 下游输出 |
| --- | --- | --- | --- |
| debug_panel.py：RosWorker | 界面订单、优先级、操作指令 | Service/Action/Topic 的界面适配 | submit 请求；将状态回传 OperatorPanel |
| order_server.py：OrderActionServer（ROS 节点） | SubmitOrder、维护请求、Action 回调 | 准入、互斥、调度、反馈、异常保护 | ExecuteOrder Goal、状态 Topic、库存 Topic |
| scheduling.py：server.queue / OrderQueue | Order、优先级、BinManager、InventoryLedger | 确定性排序、资源检查、等待与取消 | 选中的 QueuedOrder 或等待原因 |
| inventory.py：workstation.inventory / InventoryLedger | 视觉 SKU、坐标系、三维点；订单需求；执行结果 | 关联、时效、原子预留、状态提交 | 候选 ID、所有权、缺少观测数量、摘要 |
| fulfillment.py：server.bin_manager / BinManager | 订单、箱位配置、执行/取货结果 | 分配箱位、保留暂停/待取货资源、释放 | OrderAllocation、箱位状态 |
| runtime.py：server.workstation / Workstation | 订单、箱位、恢复上下文、Agent 工具请求 | 调视觉与运动、验证、更新进度、安全让出 | 工具结果、反馈事件、订单结果 |
| agent.py：本轮 Agent | 订单剩余需求、工具结果、有限工具权限 | 选择定位与操作工具，不能自己宣布装箱成功 | 工具调用；经验证的结束请求 |
| order_server.py：ActionClient / ActionServer | ExecuteOrder Goal / Feedback / Result | 连接业务调度和单次执行切片 | 队列状态与执行反馈，Watchdog 监控等待 |

它们不是八个 ROS 节点：队列、账本、箱位管理器和 Workstation 主要是 Python 对象。
调度器的 ActionClient 与 ExecuteOrder ActionServer 在同一个服务端节点内。
通过 ROS Action 保留统一的反馈、取消和结果语义；并不是直接跳过原机械臂执行链。

## 四、建议阅读顺序和重点函数

1. `order.py` 的 Order / OrderLine：不可变需求，与执行状态分开。
2. `scheduling.py` 的 QueuedOrder / submit / pending / select：如何排队，为什么不能立即执行。
3. `inventory.py` 的 observe / reserve_order / transition：怎样从观测变成可承诺资源，再变成已消耗资源。
4. `fulfillment.py` 的 allocate / finish / confirm_pickup：箱位何时占用，为什么完成不能立刻复用。
5. `order_server.py` 的 submit_callback / dispatch_pending / goal_callback / execute_callback：上面对象在哪里被串起来。
6. `runtime.py` 的 get_object_positions / pick_and_place：Agent 工具怎样落到真实定位、运动和验证。
7. `order_server.py` 的 _should_yield / _queue_result 与暂停上下文：急单怎样插入，原单怎样不重复装货。
8. 本次的 check_dispatch_timeout / _quarantine_dispatch：通信异常如何与业务资源状态关联。

学习时始终区分三种完成：Service 成功=登记成功；Action 返回 PAUSED=本次切片安全让出；
订单 COMPLETED=配货完成。最后还要确认取货，才是箱位可以再次使用。

## 五、验证范围

新增测试包含移动候选不重复承诺、预留身份位移保护、空/局部扫描区别、三类超时、
反馈刷新、迟到接收/结果/拒绝、旧回调隔离，以及执行结束不覆盖超时保护。
Qt 测试覆盖不确定任务继续显示为活动任务并禁止现场恢复。
隔离 DDS 域 71 的真实 Service/Action 集成测试使用模拟视觉和机械臂，不触及用户订单或模型 API。
真实仿真订单由用户填写，本次不自动启动订单、不清理场景。

本次结果：业务/执行回归 96 项通过；Qt 离屏界面 14 项通过；隔离 ROS 通信 1 项通过。
`colcon build --symlink-install --packages-select produce_order_workstation` 成功。
沿用原 Conda 和系统 PyQt5 环境，无新增依赖下载。代码更新和自动化测试不等同于现场订单验收。
