# 生鲜配货工作站测试总结与未覆盖风险

记录日期：2026-10-14。代码基线：`04fd55c`（一屏订单面板）。范围是 `ros2_ws/src/produce_order_workstation` 及其与 `produce_order_bt`、ROS 2、MCP、MoveIt/仿真之间的交互。本文件总结**已经有证据的测试**，并非“所有功能均已验收”的声明。

## 1. 先看结论：测到哪一层

| 测试层 | 最近一次结果 | 运行的是真实组件吗 | 能证明什么，不能证明什么 |
|---|---:|---|---|
| 工作站非 Qt 回归 | 279 通过、9 跳过；2 条第三方弃用警告 | 真实业务类、库存账本、临时 SQLite；大模型、视觉和机械技能多为替身 | 证明指定输入下的状态转换与安全约束；不证明真实相机、模型、机械臂表现 |
| 隔离 ROS 集成 | 8 通过 | 真实 ROS 2 Action/Service、MCP HTTP 和已编译 C++ 行为树；相机、模型、机械执行使用测试端 | 证明进程/协议间消息链和若干成功、失败、暂停、取消分支；不证明真实仿真抓放 |
| Qt 面板离屏回归 | 41 通过 | 真实 Qt 控件；`FakeWorker` 提供服务端反馈 | 证明按钮状态、显示和一屏布局；不证明现场服务的响应时间或实际操作全过程 |
| 完整仿真订单记录 | 4 份：3 份完成，1 份视觉校验失败 | 实际 O3DE 场景、视觉管线、Agent/BT/ROS/MoveIt 流程 | 证明这四种具体订单当时的执行结果；样本过少，不能估计稳定成功率 |

以上三组自动化测试有重叠：隔离集成用例在普通回归中通常因需要显式启用而跳过，**不能把通过数量相加为真实订单数量**。本次一屏界面改动后只重启了面板进程，未为界面改动新增真实订单，也未重置仿真现场。

## 2. 测试方法与结果明细

### 2.1 订单、库存、箱位和调度逻辑

主要用例位于 [`test_order.py`](../ros2_ws/src/produce_order_workstation/test/test_order.py)、[`test_inventory.py`](../ros2_ws/src/produce_order_workstation/test/test_inventory.py)、[`test_fulfillment.py`](../ros2_ws/src/produce_order_workstation/test/test_fulfillment.py)、[`test_scheduling.py`](../ros2_ws/src/produce_order_workstation/test/test_scheduling.py)、[`test_targeted_reliability.py`](../ros2_ws/src/produce_order_workstation/test/test_targeted_reliability.py)。

方法是创建受控订单和物品观测，调用真实 `OrderQueue`、`InventoryLedger`、`BinManager` 等业务对象，再检查队列、预留、箱位和完成数量。例如：

- 八份订单争用四个箱位：前四单占位，后续等待；确认取走一箱后等待单才可进入。
- 32 个并发预留请求争用八件账本物品：检查不会把同一件库存承诺给两个订单。
- 两份四品类混合订单争用同一批物品：检查优先级和预留互斥；未获库存的订单进入等待。
- 取消等待单释放其预留；过期或不确定的视觉观测不得当成可靠可售库存。

这里的“库存”测试对象是**程序账本和观测记录**。测试中给定的类别和坐标不是相机识别结果，所以它不能回答“真实桌面上苹果会不会漏检”。

### 2.2 感知结果处理、场景与运动接口

主要用例位于 [`test_perception_logic.py`](../ros2_ws/src/produce_order_workstation/test/test_perception_logic.py)、[`test_order_action.py`](../ros2_ws/src/produce_order_workstation/test/test_order_action.py)、[`test_dynamic_scene.py`](../ros2_ws/src/produce_order_workstation/test/test_dynamic_scene.py)、[`test_planning_scene.py`](../ros2_ws/src/produce_order_workstation/test/test_planning_scene.py)、[`test_stock_layout.py`](../ros2_ws/src/produce_order_workstation/test/test_stock_layout.py)。

方法是向工作站注入已知检测结果、候选 ID、位姿及动作反馈，或模拟场景/MoveIt 接口，再观察它是否拒绝错误数据、按期望更新规划场景。已覆盖：

- 跨品类候选落在同一表面位置时，不能重复承诺；候选视觉类别与位置冲突时，必须在机械臂运动前拒绝；旧检测 ID 不能跨新一轮感知继续使用。
- 安全的候选重选、局部歧义隔离、观测时效、抓取期间的物品身份维持，以及空检测达到次数上限后的停止。部分 `test_order_action.py` 用例还覆盖显式旧 Agent 执行模式，不能直接等同于当前默认 BT 路径的端到端验证。
- 初始不生成订单箱；接单后只为分配到的订单生成相应箱子；规划场景的桌面、箱体、障碍几何与物理配置一致。同步失败或 MoveIt 未确认碰撞对象时，不继续按“障碍已生效”执行。
- 运动 Action 的阶段反馈、取消传递、夹爪/高度参数及抬升失败后停止后续搬运。

这些测试验证的是**感知结果如何被使用**与**规划场景对象如何构造/同步**，不是 GroundingDINO 的真实识别准确率，也不是实际 MoveIt 轨迹的碰撞统计。

### 2.3 Agent、MCP 与行为树策略

主要用例位于 [`test_agent_boundary.py`](../ros2_ws/src/produce_order_workstation/test/test_agent_boundary.py)、[`test_agent_service.py`](../ros2_ws/src/produce_order_workstation/test/test_agent_service.py)、[`test_mcp_tools.py`](../ros2_ws/src/produce_order_workstation/test/test_mcp_tools.py)、[`test_fulfillment_plan.py`](../ros2_ws/src/produce_order_workstation/test/test_fulfillment_plan.py)、[`test_execution_bridge.py`](../ros2_ws/src/produce_order_workstation/test/test_execution_bridge.py)。

方法是用确定性模型响应或故意构造非法工具调用，检查它们是否能通过接口和权限校验。已检查的典型场景包括：

- Agent 只能提出本订单**剩余商品的顺序与每件视觉重试次数**；漏项、多项、超出重试上限、未知字段、上下文摘要过期会被拒绝。
- 合法策略由固定编译器生成行为树：改变 Agent 提议的商品顺序，行为树 `select → pick` 的执行顺序随之改变，最后才 `audit`。
- 业务 MCP 可以查询与提出请求，但不能绕过操作员批准直接修改订单或触发抓放。正常行为树订单的 MCP 会话只开放感知能力，物理步骤仍由 BT/工作站执行桥推进。
- 模型超时、执行会话过期、重复步骤调用、结果不确定等情况不应继续盲目运动；已确认的抓放结果可按操作 ID 查询，重复请求不得再抓一遍。

这证明了接口的**约束与失败保护**，不证明云模型总能提出好计划。更多职责边界见 [`agent_bt_planning.md`](agent_bt_planning.md)。

### 2.4 持久化、故障恢复与并发安全

主要用例位于 [`test_business_persistence.py`](../ros2_ws/src/produce_order_workstation/test/test_business_persistence.py) 和 [`test_targeted_reliability.py`](../ros2_ws/src/produce_order_workstation/test/test_targeted_reliability.py)。测试使用 `tmp_path` 建临时 SQLite，不写用户正在使用的业务数据库。

测试通过主动注入保存失败、进程重开、重复调用、并发调用等条件来检查：

- 批准订单变更时，数据库写入失败须回滚内存订单、库存预留和请求状态，不能出现“页面已改、数据库没改”。
- 维护请求区分“批准”与“真的执行成功”；明确未启动的拒绝可再次确认，运行中、失败或结果未知的操作不能自动重做。
- 抓放 `INTENT`（已开始的意图）与 `VERIFIED`（已验证的结果）分别记录。快照落后于已验证日志时可以补齐业务进度，但不能仅凭数据库推断当前物理现场安全。
- MCP 与行为树共享工作站级执行门，测试并发入口不会同时进入互斥的物理技能。
- `/clock` 的 Best Effort QoS 兼容性、未收到时钟、收包中断和时间冻结都有定向检查；失联后的任务不能因时钟恢复而自动解除隔离。

这些属于**可控故障注入**。尚未在真实仿真运动的每一个瞬间断电、杀进程和重启，因此不能声称现场恢复已做完端到端验收。问题背景和修复证据见 [`TARGETED_RELIABILITY_FIX_2026-10-14.md`](TARGETED_RELIABILITY_FIX_2026-10-14.md)。

### 2.5 ROS 2、MCP 和 C++ 行为树集成

测试文件为 [`test_bt_ros_integration.py`](../ros2_ws/src/produce_order_workstation/test/test_bt_ros_integration.py)、[`test_mcp_ros_integration.py`](../ros2_ws/src/produce_order_workstation/test/test_mcp_ros_integration.py)、[`test_queue_ros_integration.py`](../ros2_ws/src/produce_order_workstation/test/test_queue_ros_integration.py)。必须显式设置 `PRODUCE_ROS_INTEGRATION=1` 和独立的 `ROS_DOMAIN_ID=71`，避免测试节点连到用户工作站。

- C++ BT 测试启动实际编译的 `fulfillment_tree`，通过真实 `RunFulfillment` Action 与 `FulfillmentStep` Service 测试成功、Agent 计划顺序、空检测达到上限、不确定结果、暂停和取消，共六个参数场景。
- MCP 测试让独立 Agent 进程通过真实 HTTP MCP 和 ROS 2 Service/Action 交换数据；模型回复与感知/运动服务为确定性测试端，不调用云模型，也不控制现场机械臂。
- 队列测试通过真实 ROS 服务接口检查接单、顺位/优先级、执行反馈、继续调度与确认取货；工作站技能由 `FakeStation` 代替。

因此这里的“集成”是**真实通信栈 + 部分替身能力**，而非完整机器人场景。

### 2.6 操作面板与一屏布局

[`test_operator_panel.py`](../ros2_ws/src/produce_order_workstation/test/test_operator_panel.py) 的 41 项测试使用系统 Python 的 PyQt5 与 `QT_QPA_PLATFORM=offscreen`，由 `FakeWorker` 发出可重复的状态和回执，不发布真实订单。覆盖提交中/成功/超时、取消、队列清理、取货、视觉复查、恢复提示、日志/历史记录、状态刷新顺序及按钮启停。[`test_panel_queue_clear.py`](../ros2_ws/src/produce_order_workstation/test/test_panel_queue_clear.py) 另测清队列等服务请求的超时、拒绝、重复 ID、掉线和迟到回执：等待必须结束，迟到结果不得篡改新状态。

最新布局测试把窗口设为 1080×900，断言不存在整页 `QScrollArea`；提交、队列、箱位、记录、异常处理和“业务协作”操作按钮仍落在窗口可见区域。另用离屏截图人工检查过普通状态和异常状态。**列表与日志控件内部仍可滚动**；测试未覆盖所有桌面缩放比例、字体包或低于该尺寸的屏幕。

### 2.7 真实仿真订单记录

以下证据来自 `~/.local/share/produce_order_workstation/reports/` 的 JSON 报告；“完成”以报告状态及 `FINAL_AUDIT`/`ITEM_COMPLETED` 等事件为依据。本次撰写文档没有重新提交订单，也没有仅凭界面进度条推断物理成功。

| 报告文件 | 订单 | 报告结果 | 关键观察 |
|---|---|---|---|
| `agent-bt-live-20261014-a1.json` | 胡萝卜 ×1 | `COMPLETED`，1/1 | 有感知、Agent 候选选择、抓取/抬升/放置、最终审计事件 |
| `agent-bt-live-20261014-b2.json` | 玉米 ×1＋胡萝卜 ×1 | `COMPLETED`，2/2 | 两件均记录完成；两件间有返回观察姿态事件 |
| `agent-bt-live-20261014-c3.json` | 苹果 ×1＋番茄 ×1 | `FAILED`，0/2 | `CLASS_CONFLICT`：视觉类别/位置安全校验冲突，未把错误候选交给抓放 |
| `agent-bt-live-20261014-d4.json` | 番茄 ×1 | `COMPLETED`，1/1 | 有抓取、放置及最终审计事件 |

这组结果只说明**这些具体场景与订单**，不能据此计算可信的漏检率或总体成功率。尤其 `c3` 说明混合水果/番茄场景仍有实测失败。它之后的候选隔离逻辑已有自动化测试，但本记录中没有一次修复后的同场景真实仿真复测。

## 3. 尚未充分测出的风险与建议验收

按业务影响排序；“建议”是未来测试方案，不表示已经通过。

| 优先级 | 可能的问题 | 为什么现有测试不够 | 建议怎么测与通过条件 |
|---|---|---|---|
| 高 | 苹果/番茄等商品误检、漏检、遮挡导致选错候选或无法完单 | 多数测试直接注入类别和坐标；真实混合订单 `c3` 已失败 | 固定相机与桌面布局，按品类、数量、机械臂遮挡状态重复真实仿真订单；保存原图、检测框、候选筛选理由和最终实物结果。错抓应为 0，成功率与耗时需按样本统计，不凭单次通过判断 |
| 高 | 真实运动中断后业务账本与夹爪/箱内物体不一致 | 故障注入主要发生在软件替身和 SQLite | 分别在未抓、刚抓起、搬运、已放入箱但未写回等安全可控时点中断，再重启核对：不重复抓、不重复记数，状态不明时明确等待人工 |
| 高 | 连续多单、补货与取货交织时资源状态漂移 | 并发和资源互斥在模型中测过，真实场景只记录少量订单 | 在同一场景连续处理多单，穿插等待库存、补货、取消、取货；每一步比较订单状态、箱位、库存账本与实物，不能双重预留或提前复用箱位 |
| 中 | MoveIt 规划失败、轨迹不合理、碰撞或狭小箱位放置失败 | ROS 集成测试的运动端是假服务；四单样本不足 | 选择不同物品、箱位和障碍相对位置运行，保存轨迹/碰撞场景、规划耗时、执行反馈；失败必须停在可解释状态，不继续装箱记数 |
| 中 | 外部模型超时、限流、输出格式偏差导致任务长时间悬挂 | 自动化模型响应是确定性替身，不能复现真实 API 波动 | 在隔离环境注入超时、429、无工具调用、非法参数和迟到回复；检查超时、明确失败、释放占用及无重复运动，再做少量真实 API 验证 |
| 中 | ROS 服务/Action 节点重启、发现延迟或仿真时钟异常时界面与后台状态不同步 | 已有 QoS、隔离 ROS 和界面替身测试，但非完整现场联动 | 在无物品夹持的安全阶段分别重启面板、Agent、MCP、工作站；检查状态重连、订单不重复、现场恢复提示清晰；时钟失联时禁止继续运动 |
| 中 | 小屏幕、高 DPI、长订单号或较长中文错误信息裁切 | 只验证 1080×900 与几种常见状态 | 在实际显示器缩放设置下切换全部标签页与错误状态，确认按钮、请求行和完整错误说明可访问；低于 1080×900 需单独设计适配方案 |

## 4. 如何安全复现现有自动化测试

从仓库根目录 `/home/wang/rai_embodied_project` 执行。以下命令不构造真实用户订单；隔离 ROS 测试只连接域 71。不要把集成测试的 `ROS_DOMAIN_ID` 改成现场使用的域。

```bash
# 工作站非 Qt 回归；脚本负责加载现有 ROS Jazzy、Conda 环境与工作区
bash src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/check_phase47.sh unit

# 真实 ROS/MCP/BT 通信，但模型/感知/机器人是测试替身
bash src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/check_phase47.sh integration
```

Qt 测试用 `/usr/bin/python3`，不是缺少 PyQt5 的 Conda Python：

```bash
cd /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo
source /opt/ros/jazzy/setup.bash
source ros2_ws/install/local_setup.bash
export QT_QPA_PLATFORM=offscreen
export PYTHONPATH="$PWD/ros2_ws/src/produce_order_workstation:$PYTHONPATH"
/usr/bin/python3 -m pytest ros2_ws/src/produce_order_workstation/test/test_operator_panel.py \
  -q -o addopts='' --tb=short
```

如果当前终端先前加载过其他 Conda/ROS 环境，应先开一个干净的终端，避免 `PYTHONPATH` 和 `LD_LIBRARY_PATH` 混用。**真实仿真验收不由以上命令完成**：它需要运行中的场景、模型额度、人工观察和订单报告，且会改变现场库存与箱位，执行前应单独规划测试批次。

## 5. 当前总体判断

业务状态机、约束、隔离通信和界面交互已经有较系统的回归测试；这部分的证据较强。完整物理链路目前仍是**小样本、且存在一单视觉失败**，特别是视觉识别、复杂运动、长时连续多单和断点现场恢复，尚不足以称为全面验收。下一轮应优先做可重复的真实仿真测试矩阵，而不是继续增加仅使用替身的通过数量。
