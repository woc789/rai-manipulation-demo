# 数据库精简方案与交付记录

基线：`27e1dc7`，结构版本从 1 升级到 2。目标是减少重复事实和无效写入，不通过合并无关业务表掩盖复杂度。

## 实施范围

1. 移除 inventory_items.version 和申请中的 expected_database_version。
2. 新订单完成数量从 placed_items 统计；order_lines 只留需求与迁移专用 legacy_completed。order_progress 是查询视图，不复制数据。旧版日志累计数只在迁移时使用，不再参与正常重启恢复。
3. reservations 只持久保存占用、创建/更新时间、释放时间；阶段状态由 reservation_status 视图关联物品计算，移除重复维护的状态字段。
4. 保留 bin_allocations.state：失败订单与需要核对的箱位是不同事实，不能机械合并。
5. 默认不写全量历史观测。调试启用时才创建/使用历史表，并按数量保留。已有历史在迁移时保留，升级前另有完整备份。
6. 精简事件和申请 JSON 的重复标识，读取时重建兼容输出。日志按配置清理，但保护未结业务、未核对操作、待审批关联订单及关键终态事件；因此是安全优先的软上限。
7. 工作站不再直接写 SQL；直接查询收进数据访问对象。保持 ROS、MCP、行为树和界面协议兼容。

## 验证与安全边界

- 迁移在单事务中执行；先通过 SQLite backup 保存一致的升级前文件，拒绝覆盖现有备份。
- 对照旧库副本的订单、分配、完成数量及操作证据；不生成虚假的单件装箱记录来补历史计数。
- 验证装箱上限、事务回滚、独立连接竞争、断点隔离、审批、日志保护和可选观测。
- 执行工作站、Qt、ROS/MCP/行为树回归；实际仿真结果单独记录，不把替身执行作为真实运动。
- 不删除用户订单、活动预留、装箱证据或不确定操作；不自动重建旧现场。

## 最终结果

### 当前数据结构

| 数据 | v1 | v2 |
| --- | --- | --- |
| 新订单完成数量 | 明细可变计数、装箱行、操作累计 JSON 三份表达 | placed_items 单件事实；order_progress 视图统计，不再维护正常完成计数 |
| 旧数据缺少单件证据 | 与正常计数混用 | legacy_completed 专用历史基数，新订单默认零；旧总计无法归类部分仍保留 unattributed_completed |
| 预留阶段 | inventory_items 与 reservations 双写 | reservations 保存占用/释放；reservation_status 视图按物品状态计算展示阶段 |
| 库存 version | 每次更新递增但不校验 | 删除；orders.version 仍用于真实并发检查 |
| expected_database_version | 申请时写入，但审批不读取 | 删除；内容指纹与实际更新版本检查保留 |
| 全量历史观测 | 默认每次追加 | 默认不写；调试开启，最多保留配置条数 |
| 日志 JSON 标识 | 与列重复 | 表中只存一次；查询时恢复原接口形状 |
| SQL 散落 | 工作站、服务端也含查询 SQL | 收入 ResourceRepository 和 WorkcellDatabase 的明确方法 |

- 视图（VIEW）是一条可重复使用的查询，不是新增一份持久业务数据。
- 新建库默认 11 张业务表、2 个查询视图。已有库非空的 inventory_observations 保留为历史资料，因此迁移后仍可能有 12 张表；不会为减少表数丢弃旧历史。
- 保留 bins、metadata：它们小且职责明确。保留箱位状态：订单失败与箱位是否需核对可以不同。
- 保留操作的单次结果、剩余数和候选编号，用于重复请求返回原结果；不再保存或读取累计 completed_by_sku 来推动新订单进度。
- 单件装箱的数量上限由业务校验与数据库触发器共同限制，不能靠删除完成计数绕过需求上限。

### 历史保留配置

```yaml
persistence:
  record_observations: false
  max_observations: 1000
  max_events: 5000
  event_retention_days: 7
```

- 完整观测是调试选项，不是识别或抓放的必需输入。开启时自动创建历史表，每次写入后只保留最新 max_observations 条；关闭时不写入，也不删除已经存在的历史。
- 服务端每 60 秒独立清理一次日志，避免把非关键的维护操作嵌入动作事件写入事务。超过时间或数量预算的普通日志可清理。
- 保留订单失败/完成/取消和最终核验等关键事件。
- 活动订单、未释放箱位、未核对操作、待审批关联订单和需核对订单的日志不清理；存在结果不明的维护申请时暂停日志清理。
- 因此 max_events 是软上限，安全证据多时允许超过。不会为满足硬上限删除恢复依据。
- 不清理订单、操作、申请、单件装箱事实。历史日志删除不会保证数据库文件立即变小；没有自动执行 VACUUM 或破坏性压缩。

### 数据访问对象

| 对象 | 成员位置 | 作用 |
| --- | --- | --- |
| WorkcellDatabase | server.store，工作站共享同一对象 | 连接、事务、迁移、操作日志、申请、事件及历史清理 |
| ResourceRepository | server.resources / workstation.resources，同库的无状态访问对象 | 订单、明细需求、进度查询、队列、箱位和装箱证据查询 |
| InventoryRepository | workstation.inventory | 观测关联、物品状态、占用关系和单件装箱事实提交 |

- OrderActionServer、BusinessCoordinator、Workstation、ExecutionBridge 是这些对象的上游调用方，组织 ROS 请求、业务审批与物理执行，不保存另一份长期账本。
- Workstation 不再自己写已交付实体查询，服务端也不再自己写“是否存在操作”的 SQL。

### 升级、备份与保留数据

- 自动升级为 PRAGMA user_version=2，v1 正式库升级前创建同路径 .pre-v2.bak，一致性备份通过 SQLite backup 完成。
- 原有 v0 快照也可以一次性升级；旧结构定义仅用于导入/迁移，不是运行时的双后端。
- v1 已完成数量与单件证据的差额转为历史基数；不会造出虚假的物品或抓放记录。
- 重启不再从操作 JSON 提升进度。观察、操作和物理状态不明确时仍需核对，不自动重放。
- 已有备份不会覆盖；迁移失败回滚完整结构和数据，重试前需要先检查备份及失败原因。
- 切回旧代码不能继续打开新版数据库，应保留新版库，并选择与备份匹配的代码版本。

### 当前只读 SQL

```bash
sqlite3 -readonly /home/wang/.local/share/produce_order_workstation/workcell-mcp.sqlite3
```

```sql
-- 请求数量与实际完成数；不再读取 order_lines.completed。
SELECT order_id,sku,quantity,completed,legacy_completed FROM order_progress;

-- 占用关系与展示阶段；不再读取 reservations.state。
SELECT item_id,order_id,state,released FROM reservation_status;

-- 每件已验证装箱的实际证据。
SELECT order_id,sku,bin_id,entity_name,operation_id FROM placed_items;

-- 同一箱位是否仍被占用。
SELECT order_id,bin_id,state FROM bin_allocations WHERE released IS NULL;
PRAGMA user_version;
PRAGMA foreign_key_check;
```

### 验证记录

- 旧库副本升级：8 单、5 份箱位分配、8 条单件证据、14 条操作、26 条旧观测、714 条事件均保留；完成数 11 不变，3 件转为历史基数。外键和完整性检查通过。
- 新增专项覆盖：v1 升级、迁移失败回滚与备份保护、无可变完成计数、派生预留状态、默认无历史表、开启后数量限制、JSON 接口兼容、普通日志回收、活动及不确定证据保护、维护不明时停止清理、需求上限触发器、旧 REVIEW 状态保守迁移。
- 工作站回归：303 通过、9 条按条件跳过；2 条第三方弃用警告。新增专项 9 条包含在 303 中，并在最终修改后单独复跑通过。
- Qt 离屏交互：41 通过；ROS/MCP/C++ 行为树隔离集成：8 通过。后者实际运行协议与树执行，视觉、模型及机械能力使用替身，不计作真实运动测试。
- 工作站包 colcon 构建通过；变更 Python 文件基础静态检查及 git diff --check 通过。
- 正式库已备份升级并再次核对：完成数 11、历史基数 3，核心记录数量与副本一致；外键及 integrity_check 通过。备份为 `/home/wang/.local/share/produce_order_workstation/workcell-mcp.sqlite3.pre-v2.bak`。
- 真实仿真：`database-live-20261019-221623-609231`，玉米 1＋胡萝卜 1，完成 2/2，确认取货后 COLLECTED 且箱位释放。实际使用现有视觉、百炼模型、MCP、行为树与 MoveIt/O3DE。
- 只读 SQL 核对：订单只有一条；两件装箱记录、两件已消耗预留与两次 VERIFIED 抓放相符；新订单 legacy_completed 为零；没有创建 inventory_observations。
- 首轮仿真视觉复查超时，测试脚本等待就绪超时但没有提交订单；通过公开 refresh_inventory 接口复查后再测试成功。未更改视觉模型、识别布局或超时策略，不宣称解决感知冷启动/延迟问题。
- 临时数据库为 `/tmp/produce-db-v2.kvCafN/live.sqlite3`，未向正式库插入测试订单。验证结束停止临时仿真，避免后续误用测试库。
- 不包含断电、磁盘损坏、长期高负载或所有 SKU 的视觉成功率验证；不保证日志表严格低于预算，也不保证删除日志后文件立即变小。
