# 移除障碍物与数据库重启恢复实测

## 修改

- 删除 `config/demo_scene.yaml` 的 `packing_return_crate` 生成项；保留商品和四个收货箱模板。
- `config/workstation.yaml` 设置 `planning_scene.barrier.enabled: false`。
- `Workstation.prepare_scene()`：禁用时不要求、也不生成障碍物；旧场景模板即便包含配置中的障碍物名称，也从启动生成列表排除。
- `Workstation.synchronize_scene()`：禁用时不再获取障碍物实体位姿。
- `build_collision_objects()`：禁用时只生成桌面和实际收货箱的碰撞体；原有同步逻辑会 REMOVE 遗留障碍物代理。
- 不关闭碰撞检查，不改动抓取坐标、库存数量、订单容量或 MoveIt 规划算法。
- 保留安全锥源模型和原障碍物的禁用配置，便于以后接入；本次没有继续下载引擎。

## 实际重启前备份

- 正在使用的数据库：`/home/wang/.local/share/produce_order_workstation/reservation-fix-validation-20261020.sqlite3`。
- 使用 SQLite backup API 一致性备份，不复制活跃 WAL 文件、不覆盖原数据库。
- 备份：`/home/wang/.local/share/produce_order_workstation/before-obstacle-removal-20261020.sqlite3`。
- 测试单：`database-live-20261020-212833-133476`。
- 重启前：PAUSED，需人工核对；玉米、胡萝卜、苹果、番茄各完成 1，合计 4/8；A1 箱位 PAUSED，未释放。

## 重启后的实际结果

1. 使用原启动脚本重启完整仿真和面板，仍连接同一个数据库，没有切换空库。
2. 与备份逐行比较：四条商品进度、四条装箱记录、八条物理操作记录、十四条预留记录全部不变。
3. 订单变为 RECOVERY_REQUIRED，`review_required=1`；不是 COMPLETED，也没有回到 0/8。
4. A1 箱位仍绑定原订单，状态 RECOVERY_REQUIRED，`released=NULL`。
5. SQLite `integrity_check=ok`，`foreign_key_check` 无异常。
6. 实际 ROS 状态反馈：RECOVERY_REQUIRED、`startup_review=true`、`can_submit=false`，普通重置入口禁用，要求业务协作确认旧现场处理。
7. 没有自动重新抓取、重放旧物理操作或新增已装箱计数。
8. 实际相机确认中央障碍物已不存在；MoveIt 实际世界对象列表中无 `produce_workstation/route_barrier`。

## 必须区分的恢复边界

- 本次验证的是 **业务记录持久化和重启安全拦截**，不是物理场景快照恢复。
- 完整 O3DE 重启使动态生成的原商品和订单箱实体消失。目前桌面为空。
- 数据库保留“曾装箱四件”的历史，但不能据此假定四件物品仍在现场；所以不允许自动恢复抓取。
- 当前处于历史现场核对状态，未重新备货、未同步新业务场景，故 MoveIt 世界对象列表为空。不能把该空列表描述成“新工作台已准备就绪”。
- 后续需操作员明确确认旧现场处理并重建；原单历史应保留，不应被记为已交付。此次未擅自执行该确认。
- 不能声称“原八件订单已恢复并完成”；实际仍是历史进度 4/8、待核对。

## 自动化验证

- 56 项通过：动态场景生成、禁用障碍物仍保留桌面/收货箱几何、删除旧障碍代理而不误删箱位、旧障碍几何回归、现有可靠性及重启恢复测试。
- Ruff、`git diff --check` 通过。
