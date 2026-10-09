# 第 2、3 步：持续视觉库存、整单预留与安全优先级调度

日期：2026-10-08。原方案：[FULFILLMENT_ROADMAP.md](FULFILLMENT_ROADMAP.md)。
基线提交：`222363d`，开始时仅有本任务保存的方案文档暂存。
第 0、1 步按已有实现和必要测试确认，不重新进行大规模仿真订单验证。
本次没有实现第 4 步业务 Agent、第 5 步行为树或第 6 步数据库。

后续可靠性修复及模块化学习入口：[PHASE23_RELIABILITY_FIX.md](PHASE23_RELIABILITY_FIX.md)。
该补充说明更新了完整扫描后的未知候选处理，并增加调度 Action Watchdog。

## 1. 前后效果

| 环节 | 之前 | 现在 |
| --- | --- | --- |
| 接单 | 机械臂忙时拒绝新 Action | 登记 Service 接单，执行与登记分离 |
| 库存预留 | 抓取前逐件预留 | 执行前整单原子预留，抓取时仍重新视觉定位 |
| 库存不足 | 执行中才发现问题 | 等待可用观测，显示欠缺的 SKU 和数量 |
| 箱位满 | 拒绝执行 | 等待箱位，取货后继续调度 |
| 急单 | 无业务队列 | 更高优先级且资源满足时，在单件安全边界切换 |
| 普通单恢复 | 无暂停上下文 | 保留自己的箱子、实体记录、完成数量，恢复时不重复生成箱子 |
| 补货 | 主要依靠整个场景恢复 | 明确补货准备，只替换桌面货源，保留订单箱 |

## 2. 功能模块、对象与上下游

### 2.1 持续视觉候选账本

文件：`produce_order_workstation/inventory.py`。
对象：`workstation.inventory`，类型 `InventoryLedger`。

输入是 RGB-D 检测、分割、深度和 TF 得到的 SKU、坐标系、位置、时间；不使用仿真实体坐标填充观测。
输出为带观测时效和订单归属的候选记录、状态摘要、整单预留结果及缺少的可用观测数量。

- `observe()`：关联 AVAILABLE / UNKNOWN / RESERVED 的观测，预留物品再被看到时保留其标识与归属，不制造一个新可用副本。
- `reserve_order()`：一次为整份订单挑选资源。任一 SKU 不足就不提交新增预留；同一空间候选的跨类别标签不能重复承诺。
- `reserve()`：抓取前刷新同一订单已预留候选的位置，允许同所有者的幂等调用。
- `selectable()`：区分自己预留、他人预留、未知与已搬运物品。
- `reject_candidate()`：运动前安全检查拒绝的候选转为 UNKNOWN，不算库存消耗；下一次视觉观测可为订单选择其他空闲候选。
- `transition()`：沿 RESERVED → CARRYING → PLACED 提交真实执行结果。
- `release_order()`：用于未开始的预留释放；过期候选仍是 UNKNOWN，不盲目变为可用。
- `invalidate_source()`：明确补货后废弃旧货源观测，保留已装箱/取货记录并增加场景批次。

AVAILABLE 是新鲜的视觉候选，不是实物盘点保证。分类尚未验证的记录继续明确标为未验证。
单帧漏检不清零；超过 TTL 转为 UNKNOWN；业务等待原因写作“可用观测不足”，不伪装成已经确认的缺货。
只有验证搬运/装箱后才推进消耗状态，PLACED 不再作为桌面可承诺资源。

### 2.2 业务订单队列

文件：`produce_order_workstation/scheduling.py`。
对象：`server.queue`，类型 `OrderQueue`；每份记录为 `QueuedOrder`。

`QueuedOrder` 保存不可变需求 `order`、优先级 `priority`、登记顺序 `sequence`、
业务状态 `state`、等待原因 `reason`、缺少观测 `missing`、完成数量 `completed`、总数 `total` 和完成顺序 `finished_sequence`。

- `submit()`：登记唯一编号，限制优先级 0～10 和等待队列规模。
- `pending()`：优先级从高到低；同级按登记先后，包括安全暂停的待恢复订单。
- `select()`：判断箱位和整单视觉预留是否满足；否则进入 WAITING_BIN / WAITING_STOCK。
- `cancel()`：取消未开始的排队订单，不能用它偷偷释放已经装入部分商品的箱子。
- `finish()`：记录 Action 的真实业务结果；PAUSED 不等于配货完成。
- `snapshot()`：给界面提供队列和原因。

排队时不占用实际箱位。开始执行时 BinManager 分配；暂停单保留原箱位，恢复不申请新箱位。

### 2.3 ROS 接单、调度与执行适配

文件：`produce_order_workstation/order_server.py`。
节点：`produce_order_server`，类 `OrderActionServer`。

主要成员：`queue`、`bin_manager`、`workstation`、`_lock`、`_busy`、`_scene_ready`、
`_inventory_ready`、`_scan_requested`、`_dispatch_client`、`_dispatch_order_id`、`_dispatch_handle`、`_order_contexts`。

```text
界面 → SubmitOrder Service → OrderQueue
                              ↓
               空闲时视觉扫描 → 整单预留 → 调度器 ActionClient
                                                   ↓
                                    现有 ExecuteOrder ActionServer
                                                   ↓
                                    Workstation → Agent → 运动 Action
```

调度器复用原订单执行 Action；没有创建一个绕过原工作站的机械臂控制路径。
原执行 Goal 仍校验忙状态、现场、需求与箱位；默认配货模式的直接 Action 请求也必须满足整单预留。
已登记订单的执行参数必须与登记需求一致。
Mutex 保护业务准入，耗时视觉与场景维护在后台进行，由 `_busy` 阻止并发运动。
视觉扫描仅在工作台空闲时进行，每次请求只扫描一轮；失败需明确复查，不无限循环。

### 2.4 单件安全边界、暂停与恢复

文件：`runtime.py`、`agent.py`、`order.py`、`fulfillment.py`。

```text
grab → lift → 验证抓取 → place → retreat → 验证装箱
→ 提交 PLACED 与完成数量 → 检查是否应让出执行权
```

只有完成上述链路且本单尚未全部完成时，`yield_requested()` 才可能导致 `OrderPaused`。
`_should_yield()` 只允许切换到优先级更高、可获得箱位且整单预留满足的订单。
取消请求优先处理；急单不能在抓取、搬运或退出途中抢占。

`Workstation.run()` 把安全让出转换成 PAUSED，不调用失败库存处理。
服务保存已装实体、SKU 完成数量、事件及运动标记到 `_order_contexts`；箱位状态为 PAUSED。
调度器随后处理急单，再恢复普通单。恢复时复用原箱子、同步碰撞场景、恢复计数并重新视觉定位。
新 Agent 会收到已完成和剩余数量，不依赖过期的 detection_id 或旧坐标继续抓取。

PAUSED 是一段 ExecuteOrder 执行请求成功让出后的业务结果，不是整个订单完成；该段 Action 使用 succeeded，结果的 status 为 PAUSED。
暂停报告会在之后恢复执行时更新，最终完成报告及独立取货凭据保留原语义。
这些上下文只在当前进程内有效，不能宣称具备第 6 步的重启恢复。

### 2.5 补货、部分订单丢弃与异常保护

`replenish_source()` 只删除尚未装箱的桌面货源；新货源使用唯一名称，不与订单箱内实体冲突。
箱子、已装商品和障碍物不重建。补货后更新场景并重新视觉扫描，不用初始配置数量冒充观测库存。
部分补货失败则进入 RECOVERY_REQUIRED，禁止自动继续运动。

运动失败或执行通信结果不明时暂停后续调度。未知物品不能自动释放给其他订单。
如果急单失败时还存在安全暂停的部分订单，操作员可以明确确认丢弃该部分单：验证并移走其商品/箱子，保存 discard 凭据，成功后释放资源。
否则禁止恢复重置把部分订单偷偷清掉；这也避免“暂停单占位而无法恢复”的死锁。
丢弃失败保留资源，允许检查后重试。正常待取货订单仍须使用确认取货接口。

### 2.6 调试界面

文件：`debug_panel.py`（ROS 工作线程）、`operator_panel.py`（界面）、`panel_components.py`（状态显示）。

新增任务调度页：订单、优先级、状态、进度与等待原因；新增 0～10 优先级、视觉复查、补货准备、排队取消入口。
机器人执行时可继续填写新订单，登记等待与正在执行的任务展示相互独立。
补货和部分订单丢弃需要人工确认，不由 Agent 擅自触发。
新界面仍兼容旧服务的即时 Action 模式，但新版服务提供的队列模式优先使用 SubmitOrder。

## 3. ROS 接口

| 名称 | 类型 | 用途 |
| --- | --- | --- |
| `/produce_orders/submit` | 新 SubmitOrder Service | 登记需求、区域和优先级，返回登记结果 |
| `/produce_orders/cancel_queued` | 新 CancelQueuedOrder Service | 取消排队单；discard_partial 明确确认丢弃安全暂停部分单 |
| `/produce_orders/refresh_inventory` | Trigger Service | 请求空闲后的视觉复查 |
| `/produce_orders/replenish` | Trigger Service | 明确请求桌面补货准备 |
| `/produce_orders/cancel_active` | Trigger Service | 转发当前调度 Action 的取消请求；响应不是取消完成证明 |
| `/produce_orders/execute` | 原 ExecuteOrder Action | 分段执行、反馈、结果，支持安全暂停及恢复 |
| `/produce_orders/execute_motion` | 原 ExecuteMotion Action | 保持原机械臂运动执行链路 |
| `/produce_orders/workcell_status` | String JSON Topic | 增加 queue_enabled、can_register、orders、inventory_summary |
| `/produce_orders/inventory` | 原 InventorySnapshot Topic | 持续发布带时效与归属的结构化候选 |
| `/produce_orders/confirm_pickup` | 原 ConfirmPickup Service | 定向取货，释放自己的箱位并更新业务记录 |

## 4. 验证方式与范围

- 第 0、1 步复用必要回归测试，无重复大规模仿真跑单。
- 新库存/调度专项测试：原子预留、跨类别空间重复、所有者保持、过期、候选替换、排队、优先级、等待库存/箱位、取消、暂停/恢复和补货。
- 安全边界测试使用实际 Workstation.pick_and_place 方法、模拟运动结果，确认 retreat 与验证/库存提交完成之后才让出。
- Qt 离屏测试：忙时继续登记、优先级、等待原因、取消目标、完成顺序；无真实提交。
- 独立 DDS 域 71 的 ROS 集成测试：真实 Service / Action 通信，假的视觉和机械臂，验证普通单 → 急单 → 普通单恢复 → 定向取货。
- 当前仿真摄像头只读扫描：调用原 RGB-D 工具，不发运动 Goal、不创建订单、不修改现场。

只读扫描曾返回 corn 1、carrot 3、apple 4、tomato 4，共 12 个视觉候选。
这是视觉输出而非实物数量保证，不用于宣称检测准确或替代仿真订单验收。

专项测试入口：`test/test_scheduling.py`、`test/test_queue_ros_integration.py`、`test/test_operator_panel.py`。
ROS 集成测试默认跳过，仅同时设置 `PRODUCE_ROS_INTEGRATION=1 ROS_DOMAIN_ID=71` 才运行，不能接入用户现有仿真域。

新增两种 Service 已通过 colcon 构建；使用原 Conda / ROS 环境和系统 PyQt5，没有下载额外依赖。
Git 提交沿用该仓库已有 Codex 本地作者身份，不修改全局 Git 配置。

最终专项结果：库存/调度 25 项通过，Qt 界面 13 项通过，隔离 ROS 通信 1 项通过。
已有订单与运动相关回归亦已通过；没有用真实订单反复大规模复测第 0、1 步。

## 5. 真实场景验收交接

当前旧服务仍保留待取货订单，不能为启用新版本直接清场。
用户先确认取货，再重启工作台及面板，避免未交付商品丢失。
由于业务状态目前在内存中，升级不支持把旧运行进程的订单直接迁移到新进程。

由用户填写订单：

1. 提交普通混合订单；执行时再提交更高优先级订单。
2. 观察登记立即返回、队列等待原因、正常单与急单各自箱子。
3. 资源满足时，观察当前商品完成并退出后切换急单，之后恢复普通单。
4. 请求超过当前可用观测的需求，检查进入等待而非直接启动部分装箱。
5. 使用视觉复查；确认补货时检查已装订单箱没有被重置。
6. 取消未开始的排队单；按单确认取货，观察只释放指定箱位。

代码构建和自动化验证不等于真实视觉、机械臂与订单的现场验收通过。
不替用户创建真实测试订单，不自动清理旧场景，不宣称第 4～6 步已完成。
