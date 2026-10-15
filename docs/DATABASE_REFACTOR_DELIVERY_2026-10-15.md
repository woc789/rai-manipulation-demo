# 数据库统一业务状态：改造与验收

- 日期：2026-10-15。
- 改造前基线：`4b6a698`。
- 设计依据：[DATABASE_REFACTOR_PLAN.md](DATABASE_REFACTOR_PLAN.md)。
- 目标：替换业务状态的存储与一致性实现；不是给旧字典再增加一份数据库副本。

## 1. 哪些任务交给数据库

| 业务任务 | 改造前 | 现在的唯一依据 |
| --- | --- | --- |
| 订单登记、优先级、排队顺位 | 队列对象及快照 | orders、order_lines |
| 观测关联、候选状态与有效性 | InventoryLedger 中的对象 | inventory_items、inventory_observations；当前进程时钟计算有效性 |
| 商品预留与释放 | 对象的 order_id/state 字段 | reservations，与库存更新同事务 |
| 订单与箱子绑定 | BinManager 的 orders/bins | bins、bin_allocations；活动占用有唯一索引 |
| 每类商品已完成数量 | 工作站字典与检查点 | order_lines.completed，仅装箱验证成功才能递增 |
| 已装箱物品及放置位置 | 工作站列表及暂停上下文 | placed_items，与库存消耗、操作验证一起提交 |
| 人工审批与业务申请 | 已有 SQLite 申请表 | requests；与改单和释放预留共用事务 |
| 执行意图、结果与事件 | SQLite 与业务快照并存 | operations、events；与其关联的业务变更统一提交 |
| 重启恢复 | 快照重建对象，再对照执行日志 | 查询关系表及执行证据，隔离不确定物理任务 |
| 界面历史、取货状态 | 扫描 JSON 报告与回执 | ROS 发布的数据库查询结果 |

- JSON 报告仍保留：它是导出资料，不再决定订单、库存或箱位状态。
- 不再用“报告文件存在”代替订单登记或锁定。
- 界面的“清除历史”仍是隐藏显示，不删除订单、执行证据或预留记录。
- 早期仅存于报告、没有进入旧业务快照的记录仍保留为文件；本次不会凭报告推断其真实交付状态。

## 2. 哪些东西不应该交给数据库执行

- ROS Node、ActionClient、Future、线程锁和取消回调：仍属于当前进程。
- 机器人/仿真实体句柄：仍由当前场景获得；数据库只保存实体名称等基本数据。
- 视觉模型、图像、点云推理：仍由感知模块执行；数据库记录结果，不代替识别。
- 当前 Agent 对话、当前 MCP 会话、候选 detection_id：是执行上下文，不作为库存账本。
- 当前机械臂轨迹和碰撞检查：仍由 MoveIt 执行。
- 工作台忙闲、仿真时钟健康、当前派发 Future：是实时控制状态。

## 3. 模块、对象与上下游

### 3.1 数据访问底座

- 文件：`ros2_ws/src/produce_order_workstation/produce_order_workstation/database.py`。
- 对象：`WorkcellDatabase`。
- 上游：两个 Repository、BusinessCoordinator、OrderActionServer、ExecutionBridge。
- 输入：参数化 SQL、业务操作 ID、申请、事件。
- 输出：查询记录、已提交结果或明确异常。
- 主要成员：`db` 为 SQLite 连接；`_lock` 保护共享连接；`_depth` 记录嵌套层次；`session_id` 标识当前运行。
- `transaction()`：外层 BEGIN IMMEDIATE；内部 SAVEPOINT，不能提前提交外层事务。
- `read_transaction()`：多个查询看到一致的已提交视图；状态发布不会写业务快照。
- `begin_operation()/finish_operation()`：运动前的意图与之后的结果；重复 operation_id 拒绝重放。
- `event()/events_for_order()`：写入/查询审计事件。
- `create_request()/resolve_request()/admit_maintenance()/execution_result()`：保留审批及维护操作证据。
- 结构与迁移定义：`schema.py`，版本为 PRAGMA user_version=1。

### 3.2 库存与预留

- 文件：`inventory_repository.py`；对象：`InventoryRepository`。
- 替换：原来的 `InventoryLedger`，不再常驻保存 `_items`、`_executing`。
- 上游：Workstation 的视觉/抓放能力、资源调度、MCP 业务查询。
- 输入：真实视觉得到的类别、坐标系、坐标、时间；订单与物品 ID；已验证装箱证据。
- 输出：内部物品 ID、可预留候选、缺货清单、库存查询、已提交状态。
- `observe()`：执行原有空间关联与冲突检查，写观测历史和关联结果。
- `reserve_order()`：检查整单数量后统一预留，缺货时不留下部分占用。
- `begin_execution()/transition()`：确认归属后标记执行/搬运状态。
- `record_placement()`：验证归属及请求数量，统一提交装箱、消耗、进度、操作 VERIFIED。
- `snapshot()/summary()`：只读查询。这里 snapshot 的意思是“本次查询结果”，不是旧的整包持久化快照。
- `VisualRecord` 只用于一次短事务内的关联计算，调用结束即丢弃，不是第二个长期账本。
- 旧坐标重启后不能直接变成 AVAILABLE；新观测必须重新关联。

### 3.3 订单、调度与箱位

- 文件：`resource_repository.py`；对象：`ResourceRepository`。
- 替换：原 `OrderQueue` 和 `BinManager` 的容器；不再保留另一套 jobs/orders/bins 字典。
- 上游：订单 ROS Service/Action、工作站、业务审批。
- 下游：同一 WorkcellDatabase，以及共同参与调度事务的 InventoryRepository。
- 输入：Order、优先级、订单 ID、结果和核对状态。
- 输出：订单查询、待执行订单、箱位分配、缺少资源及恢复结果。
- `OrderRecord/AllocationRecord`：冻结的查询结果；不是允许直接修改的业务实体。嵌套展示数据即使被调用方修改也不会写回数据库。
- `submit()`：同 ID、同内容幂等；不同内容拒绝；顺位由数据库生成。
- `select_next()`：按优先级与顺位检查资源；库存预留、箱位绑定、DISPATCHING 在一个事务中提交。
- `update()/replace_order()`：受控更新；改单检查版本，拒绝覆盖已变化或已开始的订单。
- `cancel()/finish()`：取消释放资源；完成数必须与已经提交的明细一致。
- `pickup()/confirm_pickup()/discard_paused()`：检查允许状态并完成业务释放。
- `placements()/targets()`：为工作站恢复本单已装箱内容与目标位置。
- `recover()`：保留历史进度和归属，将旧物理任务隔离为需核对；不会自行重做抓取。

### 3.4 ROS、Agent、行为树和执行层

- `OrderActionServer`：创建一个 WorkcellDatabase，将同一对象注入 Workstation；ROS 接口保持不变。
- 登记 Service：提交事务成功后才回应“已登记”；同 ID 请求不会重复排队。
- 调度：资源事务先提交，再发送 ExecuteOrder Action；不在等待机器人时占数据库写事务。
- `BusinessCoordinator`：MCP 查询读取同一数据库；审批改单时，预留释放、明细修改、审批结果同事务。
- Agent 和 MCP：不取得 SQLite 连接；仍通过已有受约束工具访问业务与执行能力。
- 行为树：继续负责流程推进、有限重试和取消；本次没有把轨迹规划或流程决策迁入数据库。
- `ExecutionBridge`：抓放前提交操作意图；执行完成后读取已经验证的结果，不再额外保存整个工作台。
- `Workstation`：原 `_completed_by_sku/_placed_entities/_used_placement_targets` 改为数据库查询属性；不再可变累加。
- 安全暂停继续执行时，从数据库读取已完成明细与放置位置，不再维护 `_order_contexts`。
- 取货/丢弃：先记意图，再清理真实场景，最后一起提交业务释放和操作结果。中断或失败保留占用供核对。
- 恢复：物理场景准备成功且数据库更新成功后才清除恢复标记；恢复事务失败不会误报 READY。
- `operator_panel.py`：历史、队列、取货状态都来自服务端数据库投影；报告导出失败不撤销已提交业务结果。

## 4. 一件商品的完整提交链

1. SubmitOrder → orders/order_lines：登记。
2. select_next → reservations/bin_allocations/orders：资源原子预留并提交。
3. ExecuteOrder → Agent/MCP/行为树：原有计划生成与安全执行。
4. 实际视觉工具 → observe：新观测、关联和有效性检查。
5. 抓放工具 → operations.INTENT：先记录即将执行的物理操作。
6. ExecuteMotion → MoveIt → 仿真：真正移动；这段时间不保持数据库写事务。
7. 装箱验证通过 → record_placement：同事务更新库存、预留、装箱证据、明细数量、操作 VERIFIED。
8. 全单核对 → WAITING_PICKUP：订单完成，但箱位继续占用。
9. 确认取货 → 清理本单场景 → COLLECTED：事务释放箱位并保留历史证据。

## 5. 已清理内容

- 删除：`inventory.py`、`scheduling.py`、`fulfillment.py`、`persistence.py`。
- 删除：旧对象整包序列化、周期性 save/load、暂停订单业务副本和对应生产引用。
- 删除：配置中的可关闭持久化开关；生产统一使用指定数据库路径。
- 删除：GUI 从 JSON 回执判断取货、从报告目录拼接业务历史的逻辑。
- 保留：现有 ROS/MCP 接口、感知与运动算法、日志导出、UI 个人显示偏好。

## 6. 验证清单

以下区分数据库/协议测试与真实仿真，不能互相替代。

- [x] schema、外键、关系约束：拒绝给不存在订单预留物品。
- [x] 连续查询和状态发布不写业务表。
- [x] 订单登记幂等、返回对象与持久状态分离。
- [x] 整单缺货：不留下半份预留或空占箱位。
- [x] 分箱写入故障：库存、订单、分配全部回滚。
- [x] 两条独立数据库连接竞争：不能重复预留同一物品或箱位。
- [x] 嵌套事务失败：内部方法不能提前提交外层变更。
- [x] 同件装箱：数量、库存、证据和操作结果统一提交；重复提交拒绝。
- [x] 错订单操作 ID：不能拿另一订单的抓放意图提交本单装箱。
- [x] 装箱提交故障：不出现假完成，INTENT 保留以便核对。
- [x] 取消更新失败：释放操作一并回滚。
- [x] 重启：进度、箱位归属保留；旧观测和物理任务需要核对。
- [x] 旧快照迁移：备份可读、外键完整；重复装箱证据不重复生成物品。
- [x] 场景恢复：物理失败或 SQL 失败均不能清除核对标记。
- [x] 回执导出失败：已经提交的取货仍然成功。
- [x] ROS/MCP/C++ 行为树隔离集成：8 项通过；物理能力、视觉和模型使用替身，实际通信运行。
- [x] Qt 离屏交互回归：41 项通过；包括数据库历史与冲突回执场景。
- [x] 工作站非 Qt 回归：294 项通过、9 项按条件跳过；ROS 集成已另行显式开启。2 条第三方弃用警告。
- [x] 真实仿真 + 模型 + 数据库一致性：两单分别完成 1/1 与 2/2，取货释放成功。

### 真实仿真记录

| 测试订单 | 目的 | 实际结果 |
| --- | --- | --- |
| database-live-20261015-121849-122031 | 胡萝卜 1 件；真实链路、接单幂等、提交一致性、取货 | WAITING_PICKUP，1/1；取货后 COLLECTED，箱位释放 |
| database-live-20261015-122032-334529 | 不重建场景继续玉米＋胡萝卜混合单；跨单库存、逐件提交与取货 | WAITING_PICKUP，2/2；取货后 COLLECTED，箱位释放 |

- 实际使用现有百炼模型、MCP HTTP、C++ 行为树、GroundingDINO/SAM、RGB-D/TF 和 MoveIt/O3DE。
- 每单重复登记一次，数据库仍只有一条订单。
- 只读检查：完成明细总数 = placed_items 数量 = CONSUMED 预留数量 = VERIFIED 抓放数量；外键无违规。
- 使用隔离数据库 `/tmp/produce-db-audit-B541Sj/live-check.sqlite3`，没有把测试订单写进用户原业务库。
- 测试结束已停止整套临时仿真，避免继续占内存或让用户误用测试库。
- 真实测试没有覆盖所有 SKU 的长期成功率；苹果/番茄的分类混淆是已知感知局限，本次不宣称解决。
- 首轮检查发现一次启动/恢复代码块放置错误，造成 request_id 未定义；已修复，并通过后续完整回归和实际启动/订单验证。

## 7. 数据、安全边界与运行

- 默认路径：`~/.local/share/produce_order_workstation/workcell-mcp.sqlite3`。
- 临时测试可设置 `PRODUCE_STATE_DB=/绝对路径/test.sqlite3`；同一场景只启动一套工作站。
- 迁移先以 SQLite backup 创建 `.pre-relational.bak`，成功后移除旧 snapshots 表；运行时没有旧快照后端。
- 已先在用户库副本验证，再完成原库迁移：8 个订单、5 份分配、14 个操作、11 件已完成数量保留；外键和 integrity_check 检查通过。
- 原库备份：`/home/wang/.local/share/produce_order_workstation/workcell-mcp.sqlite3.pre-relational.bak`；包含迁移前一致的快照及执行记录。
- 下次正常启动，历史物理任务仍按原安全规则进入待核对；没有清空旧订单或自动将其标为交付。
- 数据库事务不能回滚机械臂已经发生的动作；本项目采用“执行意图 + 物理验证 + 不确定状态隔离”，不宣称物理 exactly-once。
- 本次没有解决视觉模型误检/遮挡，也没有保证场景重建后仍能确认旧实体实物位置。
- SQL 触发器用于注入确定的写失败，验证回滚；没有做磁盘真正耗尽、断电或文件损坏的破坏性测试。
- 没有做长时间负载、海量观测历史清理及实际运动中进程强杀后的现场对账测试；后续应独立安排。
- 使用 WAL、synchronous=FULL、外键、参数绑定、唯一/部分索引、版本条件更新；未新增 ORM 或数据库服务。
- 运行不新增依赖；临时代码检查工具 Ruff 安装在 `/tmp/produce-format-tools`，不写入 Conda 或项目运行依赖。
- 新版运行产生新业务数据后，不能仅切换旧代码就当作安全降级；应保留新库并明确选择与备份匹配的版本。

### 只读查询示例

```sql
SELECT order_id,priority,state,reason FROM orders ORDER BY priority DESC,sequence;
SELECT order_id,sku,quantity,completed FROM order_lines;
SELECT item_id,order_id,state FROM reservations WHERE state<>'RELEASED';
SELECT order_id,bin_id,state FROM bin_allocations WHERE released IS NULL;
SELECT order_id,sku,entity_name,operation_id FROM placed_items;
PRAGMA foreign_key_check;
```

### 真实仿真复测入口

- 先按原启动方式打开完整仿真，并确保使用专门的测试数据库。
- 在同一 ROS/Conda 环境运行：

```bash
python ros2_ws/src/produce_order_workstation/scripts/check_database_live.py \
  --database /绝对路径/test.sqlite3 --skus carrot --confirm-pickup
```

- 此脚本通过公开 SubmitOrder Service 登记一单，再重复登记检查幂等。
- 使用真实视觉、Agent、MCP、行为树、MoveIt；不会生成测试库存、不会重置现场。
- 完成后以只读连接检查订单数量、完成明细、装箱证据、消费预留与操作验证是否一致。
- `--confirm-pickup` 会清理这份测试订单并检查 COLLECTED 与箱位释放；不传则保留待取货。
