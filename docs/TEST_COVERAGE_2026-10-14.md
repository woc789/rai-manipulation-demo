# 生鲜配货工作站测试清单

记录日期：2026-10-14。对应代码：`04fd55c`（一屏订单面板）。这份清单回答三件事：**测了什么、怎么测、得到了什么结果**。`通过`只对列出的测试条件有效，不表示整个项目已经完成全面验收。

## 一、测试结果速览

- **工作站自动化回归：**279 通过、9 跳过；2 条第三方依赖弃用警告。真实业务类和临时 SQLite 参与测试；大模型、相机、机械技能多数由替身提供。
- **隔离 ROS 集成：**8 通过。真实 ROS 2 Action/Service、MCP HTTP、编译后的 C++ 行为树参与；模型、视觉和机械执行由测试端代替。
- **Qt 界面离屏回归：**41 通过。真实 Qt 控件参与；服务端反馈由 `FakeWorker` 模拟。
- **完整仿真订单记录：**4 份；3 份完成、1 份因视觉类别/位置冲突失败。只有这一组涉及真实仿真场景的抓放链路。
- **计数提醒：**自动化测试之间有覆盖重叠，不能将 `279 + 8 + 41` 理解成真实抓取次数；跳过的集成测试需要单独在隔离 ROS 域启用。

## 二、已执行的测试——逐条核对

### A. 订单、库存和资源

- **T01 订单输入与容量校验**
  - 方法：构造合法、非法订单，调用实际订单校验与工作站接单入口。
  - 结果：非法数量、目标和超出约束的订单被拒绝；测试不会因此启动抓放。
  - 用例：[`test_order.py`](../ros2_ws/src/produce_order_workstation/test/test_order.py)、[`test_order_action.py`](../ros2_ws/src/produce_order_workstation/test/test_order_action.py)。
- **T02 一单一箱与箱位占用**
  - 方法：构造混合商品订单，检查分箱映射、箱位分配、完成后待取货状态。
  - 结果：同一订单共用其订单箱；箱子未确认取走前不能当作空箱复用。
  - 用例：[`test_fulfillment.py`](../ros2_ws/src/produce_order_workstation/test/test_fulfillment.py)、[`test_scheduling.py`](../ros2_ws/src/produce_order_workstation/test/test_scheduling.py)。
- **T03 八单争用四箱**
  - 方法：在账本中建立八单、四个箱位，连续调度，再确认取走其中一箱。
  - 结果：箱位满时后续订单等待；取走并释放后，等待单获得箱位。
  - 用例：[`test_targeted_reliability.py`](../ros2_ws/src/produce_order_workstation/test/test_targeted_reliability.py) 的 `test_eight_orders_four_bins_wait_and_pickup_unblocks`。
- **T04 32 单并发预留八件库存**
  - 方法：16 个测试线程发出 32 次预留请求，检查每件账本物品的所有者。
  - 结果：最多八单预留成功；同一物品不会在账本中同时归属两单。
  - 边界：这里的八件是测试直接写入的观测，不是相机实际识别。
- **T05 两份四品类混合订单争用同一库存**
  - 方法：四种商品各建立一件观测，提交普通单和高优先级单并调度。
  - 结果：高优先级单先获得预留；另一单等待，不会再次占用相同物品。
- **T06 取消、过期观测与未知库存**
  - 方法：取消等待订单，或注入已过期/位置不明确的观测，再查询预留状态。
  - 结果：取消可释放预留；过期和不确定的物品不能被当作可确认库存承诺给新订单。
  - 用例：[`test_inventory.py`](../ros2_ws/src/produce_order_workstation/test/test_inventory.py)、[`test_scheduling.py`](../ros2_ws/src/produce_order_workstation/test/test_scheduling.py)、[`test_targeted_reliability.py`](../ros2_ws/src/produce_order_workstation/test/test_targeted_reliability.py)。

### B. 感知数据、安全筛选与运动接口

- **T07 候选 ID 时效与跨品类冲突**
  - 方法：注入已知类别、位置和候选 ID；模拟新一轮检测、位置重叠、类别不一致。
  - 结果：旧 ID 不能继续用；同一位置不能被两类物品重复承诺；有冲突时在机械臂运动前拒绝。
- **T08 漏检重试和候选重选**
  - 方法：注入空检测、错误候选、安全可替换候选和局部歧义。
  - 结果：重试有次数上限；可安全换候选时换候选，位置仍不确定时隔离，不盲目抓取。
  - 边界：验证的是**检测结果处理逻辑**，没有测出视觉模型的真实漏检率。
- **T09 场景生成与碰撞几何同步**
  - 方法：用模拟场景/MoveIt 接口检查桌面、箱子、障碍的几何配置和场景同步回执。
  - 结果：初始没有订单箱；接单后只生成分配箱；同步失败或规划场景未确认时不会假定障碍已经生效。
- **T10 运动 Action 与阶段安全**
  - 方法：注入运动反馈、取消、抓取后抬升失败等结果，检查后续动作和订单进度。
  - 结果：反馈可传递；取消可下传；抬升失败不能继续搬运和装箱记数。
  - 边界：部分直接技能测试覆盖旧显式 Agent 模式，不等同于当前默认 BT 路径的完整仿真验证。
  - 用例：[`test_perception_logic.py`](../ros2_ws/src/produce_order_workstation/test/test_perception_logic.py)、[`test_order_action.py`](../ros2_ws/src/produce_order_workstation/test/test_order_action.py)、[`test_dynamic_scene.py`](../ros2_ws/src/produce_order_workstation/test/test_dynamic_scene.py)、[`test_planning_scene.py`](../ros2_ws/src/produce_order_workstation/test/test_planning_scene.py)、[`test_stock_layout.py`](../ros2_ws/src/produce_order_workstation/test/test_stock_layout.py)。

### C. Agent、MCP 和行为树

- **T11 Agent 规划输入与输出约束**
  - 方法：给 Agent 一个含剩余数量、箱位和观测摘要的上下文；分别注入合法、漏项、多项、越界和过期摘要的计划。
  - 结果：只接受覆盖**剩余每一件**的顺序与视觉重试预算；非法计划在运动前拒绝。
- **T12 Agent 计划真正改变行为树执行顺序**
  - 方法：让计划把苹果排在玉米前面，解析编译后的固定行为树节点，并在隔离 ROS 测试里执行。
  - 结果：执行顺序为苹果 `select → pick`、玉米 `select → pick`、最后 `audit`；不是仅保存一段 Agent 文本。
- **T13 MCP 权限边界**
  - 方法：通过业务 MCP 查询、提出改单请求，再尝试未授权的批准和抓放调用。
  - 结果：查询/建议可用；批准须走管理器和人工确认；业务接口不能直接触发物理动作。
- **T14 模型超时、无效步骤和重复步骤**
  - 方法：让模型超时、令会话过期，或重复发送同一步骤调用。
  - 结果：失败分支退出，不因重试再次抓取已验证物品；不确定的物理结果留待核对。
  - 用例：[`test_agent_boundary.py`](../ros2_ws/src/produce_order_workstation/test/test_agent_boundary.py)、[`test_agent_service.py`](../ros2_ws/src/produce_order_workstation/test/test_agent_service.py)、[`test_mcp_tools.py`](../ros2_ws/src/produce_order_workstation/test/test_mcp_tools.py)、[`test_fulfillment_plan.py`](../ros2_ws/src/produce_order_workstation/test/test_fulfillment_plan.py)、[`test_execution_bridge.py`](../ros2_ws/src/produce_order_workstation/test/test_execution_bridge.py)。

### D. 持久化、恢复和故障注入

- **T15 订单改动的事务回滚**
  - 方法：批准订单变更时故意让 SQLite 保存失败，再检查内存订单、库存预留与数据库。
  - 结果：各处一起回滚，不出现“内存改了、数据库没改”。
- **T16 维护请求的两层状态**
  - 方法：分别注入批准成功、实际启动被拒、执行失败、运行中重启和迟到结果。
  - 结果：批准不等于执行成功；确定未启动可再次确认，结果不明不能自动重试。
- **T17 已验证抓放结果的防重放**
  - 方法：记录操作 `INTENT`/`VERIFIED`，模拟快照保存失败或进程重新打开后重发同一操作 ID。
  - 结果：可查询已验证结果并补齐落后快照；不会再抓一次；仅有 `INTENT` 时要求核对现场。
- **T18 MCP 与 BT 共享物理技能执行门**
  - 方法：并发从两个入口请求同一工作站技能，并检查 BT 会话可用工具。
  - 结果：互斥操作不会同时进入；正常 BT 订单的 MCP 会话只能使用规定的感知能力。
- **T19 仿真时钟健康**
  - 方法：用真实 ROS QoS 配置及可控时钟输入，模拟未收到、收包中断和时间冻结。
  - 结果：异常时禁止继续运动；时钟恢复本身不能解除已隔离的物理任务。
  - 用例：[`test_business_persistence.py`](../ros2_ws/src/produce_order_workstation/test/test_business_persistence.py)、[`test_targeted_reliability.py`](../ros2_ws/src/produce_order_workstation/test/test_targeted_reliability.py)。数据库测试使用临时文件，不修改用户数据库。

### E. 隔离 ROS 通信

- **T20 C++ 行为树与 ROS Action/Service：6 个分支**
  - 方法：在独立 DDS 域 71 启动编译后的 `fulfillment_tree`，运行 `success`、`planned`、`empty`、`uncertain`、`pause`、`cancel` 六组用例。
  - 结果：六组均通过；检查了成功、计划顺序、空检测上限、结果不明、暂停与取消。
  - 用例：[`test_bt_ros_integration.py`](../ros2_ws/src/produce_order_workstation/test/test_bt_ros_integration.py)。
- **T21 独立 Agent 进程 → MCP HTTP → ROS 通信**
  - 方法：启动独立 Agent 测试进程和真实 MCP/ROS 接口；用确定性模型、模拟感知和运动端响应。
  - 结果：通信链路用例通过；不调用收费云模型，也不控制现场机械臂。
  - 用例：[`test_mcp_ros_integration.py`](../ros2_ws/src/produce_order_workstation/test/test_mcp_ros_integration.py)。
- **T22 ROS 队列服务与调度反馈**
  - 方法：通过实际 ROS 接口提交、读取反馈、继续调度、确认取货；工作站技能由 `FakeStation` 代替。
  - 结果：队列通信集成用例通过；不能据此断言真实抓放成功。
  - 用例：[`test_queue_ros_integration.py`](../ros2_ws/src/produce_order_workstation/test/test_queue_ros_integration.py)。

### F. 订单面板

- **T23 提交、取消、清队列与迟到回执**
  - 方法：`FakeWorker` 发出提交中、成功、超时、离线、拒绝和迟到回执；模拟用户点击对应按钮。
  - 结果：按钮和提示随状态更新；请求超时后不永远卡住，迟到回执不覆盖新状态。
- **T24 取货、恢复、日志和异常引导**
  - 方法：给界面输入待取货、库存错误、需现场核对等状态，检查可用按钮和引导文本。
  - 结果：待取货箱须确认取走；恢复不越过现场核对保护；清日志不清原始报告。
- **T25 一屏布局**
  - 方法：离屏创建 1080×900 窗口，检查不存在整页滚动容器；检查提交、队列、箱位、记录、异常操作和业务协作按钮坐标。
  - 结果：控件在窗口内；另外人工检查了普通和异常状态的离屏截图。列表/日志内部仍可滚动。
  - 边界：未覆盖小于 1080×900 的屏幕、所有字体或高 DPI 缩放。
  - 用例：[`test_operator_panel.py`](../ros2_ws/src/produce_order_workstation/test/test_operator_panel.py)、[`test_panel_queue_clear.py`](../ros2_ws/src/produce_order_workstation/test/test_panel_queue_clear.py)。

## 三、真实仿真订单——逐单记录

证据目录：`/home/wang/.local/share/produce_order_workstation/reports/`。下列结果从报告状态及事件读取；不是仅凭 UI 进度条判断。本次整理清单**没有重新发布订单**。

- **L01 胡萝卜 ×1**：`agent-bt-live-20261014-a1.json`；`COMPLETED 1/1`。有感知、候选选择、抓取、抬升、放置和最终审计事件。
- **L02 玉米 ×1＋胡萝卜 ×1**：`agent-bt-live-20261014-b2.json`；`COMPLETED 2/2`。两件均有完成记录，中间有返回观察姿态事件。
- **L03 苹果 ×1＋番茄 ×1**：`agent-bt-live-20261014-c3.json`；`FAILED 0/2`。原因是 `CLASS_CONFLICT`（视觉类别/位置冲突），候选在运动前被拒绝。
- **L04 番茄 ×1**：`agent-bt-live-20261014-d4.json`；`COMPLETED 1/1`。有抓取、放置和最终审计事件。
- **L05 结论边界**：四单不是可靠的成功率样本；尤其苹果＋番茄场景实测失败。后续候选隔离逻辑有自动化回归，但这份记录中没有其修复后的同场景仿真复测。

## 四、尚未充分测试——按优先级列出

- [ ] **U01 真实视觉稳定性（高）**：各品类、数量、遮挡与机械臂姿态组合；保留相机原图、检测框、候选筛选理由和抓取实物结果。当前注入式测试不能给出漏检/误检率。
- [ ] **U02 运动中断与现场对账（高）**：分别在未抓、刚抓起、搬运、已放箱未写回的安全可控时点中断/重启；确认不重复抓、不重复记数，状态不明须人工核对。
- [ ] **U03 长时连续多单（高）**：在同一场景交错执行排队、缺货、补货、取消、取货；逐步比对订单、箱位、库存账本与实物，检查资源不漂移。
- [ ] **U04 真实 MoveIt 轨迹与避障（中）**：改变物品、箱位、障碍相对位置；记录规划耗时、轨迹与碰撞场景、执行反馈，验证失败后不继续装箱记数。
- [ ] **U05 真实模型服务异常（中）**：模拟/验证限流、超时、无工具调用、非法参数和迟到回复；检查明确退出、资源释放与不重复运动。
- [ ] **U06 全栈节点重启与时钟故障（中）**：在安全静止阶段分别重启面板、Agent、MCP、工作站；核对重连、订单状态和现场恢复提示。当前仅有分层测试。
- [ ] **U07 更多显示环境（中）**：小屏幕、高 DPI、长订单号及长中文错误；当前一屏验收只覆盖 1080×900 等少量状态。

## 五、复现命令与安全边界

- **R01 工作站回归：**在 `/home/wang/rai_embodied_project` 执行：

  ```bash
  bash src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/check_phase47.sh unit
  ```

- **R02 隔离 ROS 集成：**同一目录执行；脚本自动使用 `ROS_DOMAIN_ID=71`，不要改成现场工作站所在的域：

  ```bash
  bash src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/check_phase47.sh integration
  ```

- **R03 Qt 界面测试：**从干净终端执行；系统 Python 才有当前所需的 PyQt5：

  ```bash
  cd /home/wang/rai_embodied_project/src/examples/rai-manipulation-demo
  source /opt/ros/jazzy/setup.bash
  source ros2_ws/install/local_setup.bash
  export QT_QPA_PLATFORM=offscreen
  export PYTHONPATH="$PWD/ros2_ws/src/produce_order_workstation:$PYTHONPATH"
  /usr/bin/python3 -m pytest ros2_ws/src/produce_order_workstation/test/test_operator_panel.py \
    -q -o addopts='' --tb=short
  ```

- **R04 真正的完整仿真验收：**上述命令不执行真实订单。仿真订单会改变库存与箱位、可能调用收费模型，需单独安排订单、观察和现场恢复步骤。

**总体判断：**业务逻辑、通信和界面已有较系统的自动化回归；真实视觉、运动、长时多单和中断恢复仍缺足够的端到端证据，因此目前**不能称为全面测试完成**。
