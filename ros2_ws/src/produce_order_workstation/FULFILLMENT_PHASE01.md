# 有限库存多订单配货：第 0、1 步

后续修正：本记录描述 `0e26bf3` 的第一版。当前默认配货模式已改为接单才生成箱子、
确认取货移走整箱；固定四箱和只删商品的说明由 `DYNAMIC_BOX_SCENE.md` 替代。

日期：2026-10-08。基线：`13c8e87`，开始时工作树干净。
范围：基线与接口设计 + 单订单单箱。不是完整的多订单/持续库存版本。

## 1. 需求与旧行为对比

| 方面 | 原分类工作站 | 本阶段默认配货模式 |
| --- | --- | --- |
| 箱子归属 | 玉米 A1、胡萝卜 A2、苹果 A3、番茄 A4 | 一份订单绑定一个空闲箱位，混合商品同箱 |
| 配货结束 | 自动清场并重建库存 | 商品保留，箱位 WAITING_PICKUP |
| 下一单 | 全新满库存场景 | 使用另一个空箱，桌面剩余商品不变 |
| 人工取货 | 不存在 | 按订单确认，只删除该订单商品，再释放箱位 |
| 补货/恢复 | 重置场景 | 明确确认后重建；未取走完成订单时禁止 |
| 失败后箱位 | 没有业务归属 | 未运动释放，运动后锁定待恢复 |

四个仿真箱仍是固定接货位置。“确认取货”代表人工取走商品并换回空箱，
不是新增机械臂搬箱动作。当前小箱配置仍为 4 件，配货模式是整单最多 4 件。
放置使用不同释放偏移，不是三维装箱优化，也没有证明任意 4 件长条商品一定能稳定堆放。

原分类模式可通过 `operation_mode: classification` 恢复，但不恢复自动清场的旧业务行为。

## 2. 第 0 步：对象、状态、接口设计

### 已落实的对象

- `Order` / `OrderLine`：不可变订单与 SKU 数量。复用原对象。
- `BinResource`：`bin_id`、`zone`、`index`、`state`、`order_id`；表示物理箱位的业务归属。
- `OrderAllocation`：`order`、`bin_indices`、`state`、`entities`；关联订单、箱位和经过验证的装箱物品。
- `BinManager`：分配与生命周期规则，由订单服务器的锁串行保护；不是 ROS 节点。
- `Workstation`：保留视觉定位、抓放、MoveIt Action 客户端和仿真安全验证。

`entities` 是取货清理/结果验证用的仿真实体引用，不作为 Agent 观测，不提供抓取坐标。

### 库存对象的后续接口约束（尚未实现）

未来 `InventoryItem` 至少需要物品标识、类别、带坐标系和时间戳的位置、
观测有效性、available/reserved/carrying/placed/unknown 状态及关联订单。
不得用一帧漏检直接清零库存，仿真真值也不得作为视觉观测填入库存。
本阶段没有库存账本和库存预留；配置里的 initial_counts 只是初始场景数量。

### 箱位与订单状态

```text
FREE --接单分配--> ALLOCATED --整单校验成功--> WAITING_PICKUP
                                                     |
                        人工确认 + 定向清理成功        |
FREE <-----------------------------------------------+

ALLOCATED --动作前失败/取消--> FREE
ALLOCATED --动作后失败/取消--> RECOVERY_REQUIRED
RECOVERY_REQUIRED --显式重置成功--> FREE
```

订单分配记录取货后标记 COLLECTED；重置丢弃失败订单标记 DISCARDED_BY_RESET。
配货 Action 的 COMPLETED 是执行完成，不是已经交付；报告附带 delivery_state。
工作台 READY 可以同时存在 WAITING_PICKUP 的箱子，是否能接单还取决于空闲箱位。

### ROS 接口

| 接口 | 类型 | 输入 → 输出 | 上下游 |
| --- | --- | --- | --- |
| `/produce_orders/execute` | 原 ExecuteOrder Action | 订单 → 阶段反馈和执行结果 | 面板 → 订单服务器 |
| `/produce_orders/workcell_status` | 原 String Topic，transient-local | state/reason/can_submit/can_recover/can_pickup/operation_mode/bins JSON | 服务器 → 面板 |
| `/produce_orders/confirm_pickup` | 新 ConfirmPickup Service | order_id → success/message | 面板人工确认 → 服务器定向清理 |
| `/produce_orders/recover` | 原 Trigger Service | 显式重建请求 → 接收结果；后续状态由 Topic 发布 | 面板 → 场景恢复线程 |
| 原运动 Action | 不变 | 四阶段目标 → 运动反馈与结果 | Workstation → C++ → MoveIt |

不在本阶段新增尚无消费者的库存 ROS 消息，也不引入行为树或数据库依赖。

## 3. 第 1 步：模块联动与修改细节

### 接单：order_server → BinManager

`goal_callback` 先校验订单、场景、忙状态和重复 ID；在同一把锁下分配箱位，
成功才接受 Action。配货模式选择第一个空箱，分类模式继续用固定 SKU 箱。
机械臂仍只接受一份正在执行的订单；完成的订单可以同时占用其他箱位，
这不是多订单队列。报告或运行内历史存在的 ID 不允许重复接收。

### 执行：Workstation → 原 Agent → 原 ROS 运动链

`begin_order(order, bin_index=...)` 接收服务器分配的箱位，只清理当前订单的
候选、计数和临时状态，不清理现场。`_delivered_entity_names` 跨订单保留已装箱
实体名，防止箱内商品被安全检查当成新货源；它是隐藏验证记录，不是视觉库存。

`plan_placements(..., bin_index=...)` 在配货模式把整单展开成商品序列，
使用整单级别的不同偏移，再按 SKU 查找当前第几件的目标。
旧版是各 SKU 独立计算偏移；直接改箱号会导致不同 SKU 重复投放同一点，因此同时改了偏移规则。

Agent 仍调用三个原工具，工具说明和订单提示改为“工作站分配的箱子”，
模型无权挑箱或传坐标。抓取坐标继续来自 RGB-D 工具，机械臂仍执行
grab → lift → place → retreat，路径仍由 MoveIt 规划。

### 结束：执行结果 → 箱位状态 → UI

订单成功后 `BinManager.finish` 保存验证过的物品和箱位，标记 WAITING_PICKUP；
删除了自动补货线程。报告增加 operation_mode、bin_ids、delivery_state。
失败若尝试运动，标记 RECOVERY_REQUIRED，保守阻止继续下单，不能用“取货”绕过恢复。

### 取货：UI → ConfirmPickup → Workstation → BinManager

面板列出待取货订单；二次确认后发送 order_id。服务器拒绝忙状态、未知订单和未完成订单。
`collect_order` 在删除前检查所有仍被桥接器跟踪的订单物品都在目标箱内，
只对这些物品调用现有 O3DE delete_entity 适配器。不删除箱子、障碍物、其他订单商品或桌面库存。
删除成功后保存独立 `.pickup.json` 回执，再释放箱位。
部分删除失败时保留归属；重试跳过此前已成功从桥接器删除的物品。
仿真实体跟踪与删除成功响应是该重试机制的依据，不是 TF 残留消失检查。

### 显式重建：recover → prepare_scene

只在机械臂空闲且没有待取货/分配中箱位时允许；面板弹窗告知会清理桌面及失败订单。
成功重建后释放失败箱位；失败时继续锁定，不发布 READY。此操作模拟“重新备货并恢复工作台”，
不是逐件补货，也不是自动库存对账，后续阶段再实现这两项。

## 4. 技术与边界

- 领域对象/状态机：将箱位业务归属从 Workstation 的运动逻辑分离。
- ROS 2 Service：短请求/明确响应的人工取货确认；实际执行仍使用长任务 Action。
- Topic + transient-local QoS：后打开的面板可收到最新箱位快照。
- 锁与资源排他：接单分配、取货、恢复互斥，禁止运动中删除物品。
- 定向清理、重试和审计：删完且记录成功后才释放箱位；执行报告与交付回执分开。
- 模式兼容与回归测试：原分类运动测试保留，新配货规则另测。

本阶段箱位记录仍在内存里，没有 SQLite、持续库存、队列、业务 Agent 或行为树。
重启整个 launch 会重新初始化仿真，不能算业务恢复；不能在有待取货订单时把重启当成正常操作。
只重启服务器也不支持接管旧场景，仍需后续的持久化与物理对账设计。

## 5. 验收方法

自动测试覆盖：混合单单箱、整单偏移、容量、满箱拒单、归属、失败锁定、
禁止清除未取货订单、取货失败可重试、忙状态拒取货、旧分类兼容、定向物品删除。
仿真订单由操作员填写，未自动提交任何测试订单，也未调用付费模型做验收。

人工验收：

1. 输入四类各 1 件，共 4 件，确认预览同一个箱子。
2. 提交后观察真实抓放，整单成功后 A1 为 WAITING_PICKUP，现场不重建。
3. 有剩余商品时再提交下一单，箱位应分配 A2（具体库存可用性由视觉决定）。
4. 选择上一单并确认取货，仅它的箱内商品消失，A1 变为 FREE。
5. 有待取货订单时，恢复按钮不可用，直接调用恢复服务也被拒绝。
6. 所有完成订单取货后，才使用补货/恢复重新备货。

四件混合商品的真实抓取/堆放效果需以上人工订单验证；单元测试不能替代物理验收。

## 6. 本次验证记录

- `produce_order_interfaces` 与 `produce_order_workstation` colcon 构建成功，新增取货服务生成成功。
- 原有 48 项回归与新增 19 项测试：共 67 项通过。
- Qt 离线检查通过：混合单预览、整单超量禁提交、待取货订单选择与按钮状态；未启动 ROS worker。
- 2026-10-08 16:22，完整 launch 完成视觉服务初始化与规划场景同步，日志确认每类初始 4 件库存就绪。
- 实际 ROS 图确认 `/produce_orders/confirm_pickup` 的类型为 `produce_order_interfaces/srv/ConfirmPickup`；
  状态消息新增 operation_mode/bins，四箱初始为空闲。
- 没有安装新的第三方包，复用 Jazzy、rai-jazzy Conda、系统 PyQt5 与现有模型缓存。
- 没有自动提交订单、移动机械臂或调用模型 API；真实订单抓放/取货验收等待操作员执行。
