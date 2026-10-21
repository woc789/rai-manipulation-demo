# 运行中补货与原订单续跑修复

## 解决什么问题

- 保留订单箱后，箱体可能遮住部分源商品。
- 原补货流程调用 `_replenish_stock()` 后强制 `_initial_inventory_required=True`，复用首次场景的“四类各 4 件全部视觉确认”条件。
- 即使数据库已确认 2 个苹果、原订单只差 1 个苹果，整站仍报 INVENTORY_ERROR。
- 补货成功后也没有申请解除安全暂停单的待复查标记，可能一直不能进入调度。

## 改动位置与上下游

1. **补货入口**：ROS 服务 `/produce_orders/replenish` → `OrderActionServer.replenish_callback()` → `_replenish_stock()`。
2. **实体与旧观测处理**：继续复用 `Workstation.replenish_source()`；保留订单箱和已装商品，替换源商品，调用 `InventoryRepository.invalidate_source()` 退役旧来源观测、释放未消耗预留、推进库存代次。
3. **补货后的扫描策略**：`_replenish_stock()` 设置 `_initial_inventory_required=False`、`_inventory_ready=False`、`_scan_requested=True`、`_resume_review_after_scan=True`。生成成功不是配货成功，也不是视觉库存就绪。
4. **视觉库存更新**：`_scan_stock()` → `Workstation.scan_inventory()` → 原有检测/分割/深度/TF → `InventoryRepository.observe()` → SQLite。只记录实际观测，不凭生成清单补齐可用数量。
5. **安全暂停恢复**：扫描成功后，仅清除 PAUSED 单的待复查标记；不修改订单编号、需求、完成量、装箱证据和箱位绑定。不清除 RECOVERY_REQUIRED/EXECUTION_UNCERTAIN 隔离。
6. **重新准入**：原调度器调用 `ResourceRepository.select_next()` → `InventoryRepository.reserve_order()`，根据已经装箱的物品扣除已满足需求，仅为剩余需求预留新观测。
7. **观测不足**：该订单留在 WAITING_STOCK；整站 READY 表示可以进行调度检查，不表示库存充足，更不表示全部订单能立即执行。
8. **物理不确定**：成功返回视觉数据也不能消除运动不确定性；在清除暂停待复查标记之前检查 `physical_state_uncertain`，保留隔离并进入 RECOVERY_REQUIRED。

首次准备/全场重置的全量验收没有删除，仍要求配置的初始数量被确认；本次仅把运行中补货与首次准备分开。

## 隔离测试：测什么

- 新文件 `test_replenishment_resume.py` 共 7 个用例（含参数化），视觉与实体接口是测试替身，订单、预留、装箱和箱位操作使用真实 Repository 与隔离 SQLite；不把它们称为真实仿真。
- 0 个苹果观测：扫描可完成，原单变 WAITING_STOCK，缺少量恰为 1，不自动宣告完成。
- 1 个苹果观测：原单 5/6 保留，只预留 1 个苹果即可重新准入，不要求全类各 4 件。
- 2 个苹果观测：同样只预留剩余 1 件，原五条装箱记录逐字段不变。
- 扫描抛异常：不解除安全暂停的待复查标记，不进入执行。
- 运动不确定：即使扫描接口成功，也不能解除核对。
- 同时存在未知状态订单和待取货订单：未知状态不解锁，待取货订单及箱位不释放。
- 生成补货失败：进入 RECOVERY_REQUIRED，不申请自动续跑。
- 保留原首次全量验收、库存事务、调度、恢复与 Action 回归测试。
- 一条旧场景测试仍断言已删除障碍箱存在、实体总数为 21；改为按障碍启用配置断言，禁用时为四箱模板与 16 件源商品。该历史测试漂移与本次补货逻辑无关。
- 最终回归：**158 passed，2 条第三方弃用警告，39.00 秒**。Ruff 与 `git diff --check` 通过。

## 真实仿真：实际发生了什么

- 使用同一个运行数据库，修复前通过 SQLite backup 保存至 `before-replenishment-gate-fix-20261021.sqlite3`。
- 完整重启加载新代码；由已有仿真检查点恢复机制恢复现场，没有创建新订单。
- 原单 `stress-five-20261021-103753-r3`：玉米 1、胡萝卜 2、苹果 2、番茄 1。
- 原进度 5/6，仅差 1 个苹果；Agent/行为树/MoveIt 实际完成剩余抓放，2026-10-21 11:02:46 达到 **6/6，WAITING_PICKUP**。
- 与备份逐字段比对：原五条装箱记录身份、操作编号、来源、目标与确认时间均保留；最终 VERIFIED PICK_AND_PLACE=6。
- 保留 A1 箱、不确认取货，再调用真实补货服务；11:03:08 视觉确认玉米 4、胡萝卜 4、苹果 2、番茄 4，系统正常返回 **READY**，`initial_inventory_check=null`。
- 补货后 SQLite 库存代次增加 1；原 6 条装箱记录与完整箱位绑定不变；订单仍 WAITING_PICKUP，未被自动取走。
- `integrity_check=ok`，`foreign_key_check` 无错误。
- 实测脚本：`scripts/check_replenishment_live.py`，只监测已有订单并调用补货，不提交新单、不取货、不重置、不直接写业务数据库。重启检查点中的 PAUSED 且 review_required=false 是可调度过渡状态，不被当作执行失败。

## 不宣称已经验证的部分

- 原单的本次 5/6 → 6/6 是**完整重启后的检查点续跑**；同进程补货自动解除安全暂停并重新准入的分支由隔离测试覆盖，不把两者混称为同一次真实物理过程。
- 保留一个箱子的实际补货已经通过；四箱占满可能产生更严重遮挡，仍须正式测试。
- “四箱位为一轮、共五轮二十单”的正式压力测试尚未完成，本次修复不能冒充该验收通过。
- 本次没有更换视觉模型，也没有解决所有遮挡与漏检；Agent 偶发未返回恰好一个提案是另一个已记录的问题，没有在本次改动中掩盖。
