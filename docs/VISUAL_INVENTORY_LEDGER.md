# 下一步：视觉候选台账与单件预留

这是库存管理方向的第一块基础，不是完整的仓储系统。它解决“Agent 看到了什么、选择了哪件、这件被谁占用、是否已经搬入订单箱”不能连续追踪的问题；不新增前端包装，也不改变视觉模型。

## 整体输入输出

```text
订单 Action → Workstation → Agent 选择感知/抓放工具
                                ↓
RGB 检测 + 分割 + 深度 + TF → 当前观测坐标
                                ↓
Workstation.get_object_positions → InventoryLedger.observe
                                ↓ 候选 ID
Agent.pick_and_place → 再读取视觉坐标 → reserve
                                ↓
运动 Action → MoveIt → 仿真 → 既有抓取/放置结果校验
                                ↓
InventoryLedger.transition → 订单服务发布类型化快照
                                ↓
/produce_orders/inventory → RosWorker → Qt“视觉库存”
```

台账不从桌面生成配置或仿真实体坐标预填。初始为空；只有视觉工具返回候选坐标才创建条目。原有仿真结果校验仍是安全/结果检查，不变成感知坐标来源。

## 1. 台账模块：InventoryLedger

文件：`produce_order_workstation/produce_order_workstation/inventory.py`。

`InventoryItem` 保存候选编号、SKU、三维位置、坐标系、观测墙钟时间、单调时钟时间、状态、所属订单、分类确认标志和观测来源。`classification_verified=false` 明确说明 SKU 仍是视觉预测。

`InventoryLedger` 成员 `_items` 管理条目，`_serial` 生成编号，`epoch` 区分场景批次，`RLock` 保护 ROS 多线程回调与订单执行线程的共同访问。默认同 SKU、同坐标系、3.5 cm 内关联；一帧内一对一匹配。它是近邻关联，不是精确的实体追踪器，不跨 SKU 保证唯一身份。

| 方法 | 上游输入 | 输出/作用 |
|---|---|---|
| `observe` | 视觉 SKU、frame、坐标列表 | 创建或更新候选，返回候选 ID |
| `reserve` | 候选 ID、订单 ID、新鲜视觉坐标 | 原子检查并标记单件预留，拒绝重复占用 |
| `transition` | 候选 ID、所有者、目标状态 | 仅允许合法搬运状态转换 |
| `finish_failed` | 订单 ID、是否尝试过运动 | 未运动则释放；运动后状态不确定则锁为 UNKNOWN |
| `collect` | 已确认取货的订单 ID | 已装箱条目转为 COLLECTED |
| `snapshot` | 无 | 返回隔离副本、观测年龄与过期标志 |
| `reset` | 显式重建桌面成功 | 清空旧候选并增加 epoch |

正常状态为 AVAILABLE → RESERVED → CARRYING → PLACED → COLLECTED。AVAILABLE 超过 60 秒未更新转 UNKNOWN；新视觉可以刷新无主候选。发生运动后的失败候选保留所属订单，不擅自释放，需要场景恢复处理。漏检不会删除条目，更不能推断“真实库存为零”。

## 2. 工作站整合

`runtime.py` 的 `Workstation.inventory` 持有台账；`_inventory_candidates` 把单次感知的 detection ID 映射到候选 ID。

- `get_object_positions` 的 RGB-D/TF 结果进入 `observe`，工具返回值增加 `inventory_item_id`，原检测 ID 继续兼容 Agent 调用。
- `pick_and_place` 保留原来的二次视觉读取与安全检查，拿到新鲜坐标后才预留。抬起校验通过转 CARRYING；放入箱内校验通过转 PLACED。每次都输出事件，便于订单报告追溯。
- 取消/失败执行保守释放规则；取货成功才 `collect`；下一单不清空全部台账。
- 一件放置成功不代表整单成功；最终订单审计与箱位状态仍由原订单模块负责。

Agent 不获得伪造库存，也没有新加绕过视觉的工具。现在是“逐件预留”，并没有实现整单库存预留、多订单队列或并行机械臂执行。

## 3. ROS 数据接口

`produce_order_interfaces/msg/InventoryItem.msg` 定义单候选字段，`InventorySnapshot.msg` 定义 header、epoch、scene_ready、persistence 和 items。

`order_server.py` 的 `publish_inventory` 每次发布工作台状态时同步发布 `/produce_orders/inventory`，约 1 Hz，depth=1、TRANSIENT_LOCAL，新订阅者可收到最后一份快照。header 是发布时间；observed_unix_sec 是工具返回时记录的墙钟时间，**不是相机曝光时间**。age 用单调时钟计算，避免墙钟调整破坏 TTL。

`persistence=in_memory` 表示进程重启不恢复候选占用，不应当成数据库。重启工作台同时重建仿真场景，因此不会把旧候选误用到新场景。

另外修正运动事件 Action feedback 的 completed/total 默认值：以前中间阶段可能显示 0/0，现在取订单总件数与实际完成统计，不制造定时器进度。

## 4. 调试界面

`debug_panel.py` 的 `RosWorker` 在线程内订阅类型化 ROS 消息，通过 Qt signal 给主线程；`operator_panel.py` 只渲染，不控制库存真值。侧栏“视觉库存”和下方“视觉台账”展示 ID、预测类别、状态、位置、时效、订单。

界面明确标注“候选数量不等于实物库存”。历史 PLACED/COLLECTED 坐标是最后一次视觉观测的源位置，不是箱内实时坐标；陈旧条目保留过期标志。

## 验证及后续

单测覆盖重复观测关联、坐标系隔离、一对一关联、漏检不清零、TTL、预留所有权、非法转换、并发竞争、失败保守处理与取货；UI 测试检查未确认库存标注。

部署后真实混合订单 `integration-20261008-193526` 验证了三件商品的 RESERVED → CARRYING → PLACED，以及番茄抓取失败时的 RESERVED → UNKNOWN、保留原订单归属。通过真实 `/produce_orders/inventory` 订阅确认，而不是界面注入假数据。最终订单 FAILED 3/4：番茄运动规划/执行全部通过，但抬起校验失败；这次不能宣称新阶段完整订单已成功。

之后公开恢复服务重建场景，epoch 从 1 增至 2；复测 `integration-20261008-193840` 最终 COMPLETED 4/4。台账四件商品均为 PLACED；公开取货服务成功后四件转 COLLECTED，箱位释放。失败记录仍保留，没有把前一单改成成功。

番茄视觉源位置为 (0.358278, 0.330271, 0.040182)，仿真校验的抓取前/后高度为 0.029022/0.029192 m，仅变化约 0.17 mm，没有达到 6 cm 的抬起条件。源位置与仿真位置存在约 1.36 cm 的 XY 差；这是诊断证据，不能直接判定为唯一原因，也没有用仿真真值替换视觉坐标或放宽抬起检查。

本阶段不宣称已实现数量核验、持久化恢复、盘点、缺货采购、补货或调度。后续应先验证感知候选和实际商品的一致性，再考虑整单预留与 ROS 任务队列；否则把不可靠观测当库存会造成错误拒单。
