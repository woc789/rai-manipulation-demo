# 配货工作台界面重构

日期：2026-10-08。基线：`7cae1e3`。本次只重构桌面交互，不更改订单分配、Agent、视觉、MoveIt 或仿真场景。

## 设计依据与范围

参考 Shopify Polaris 的业务资源分组和 IBM Carbon 的数据表指南：相关操作放在一起，状态与操作就近，短标题，详细技术内容按需展开。没有复制其代码，也没有引入浏览器、前端框架或额外依赖。

- [Carbon 数据表指南](https://www.carbondesignsystem.com/building-blocks/core/components/data-table/guidelines)
- [Polaris 资源列表布局](https://polaris-react.shopify.com/patterns/resource-index-layout)

配色为深绿导航 + 浅色背景 + 白色卡片，商品图标使用 QPainter 原生绘制。默认 1280×900，可缩到 1024×768；较小窗口纵向滚动，侧边导航可直接跳到记录/日志。避免长篇说明占据操作区域。

## 区域与实际数据

| 区域 | 作用 | 数据来源 |
| --- | --- | --- |
| 顶部指标 | 工作台状态、执行中、待取货、空箱位 | `/produce_orders/workcell_status` |
| 新建订单 | 四类商品数量、简短校验、预计箱位、提交 | 本地草稿 + 原有 `validate_order`，正式箱位仍由服务器分配 |
| 任务进度 | 订单号、当前阶段、实际完成数、取消 | `ExecuteOrder` Action 的 acceptance / feedback / result |
| 订单箱位 | A1–A4 的空位、分配、待取货、需要恢复 | 工作台发布的真实 bins 快照 |
| 确认取货 | 选择待取货订单并确认 | `/produce_orders/confirm_pickup`，有二次确认 |
| 订单记录 | 最近最多 50 条真实报告 | 本地报告 JSON，取货回执及当前箱位快照 |
| 事件 / 技术日志 | 简短事件与完整 feedback JSON 分开 | 原有工作线程信号 |
| 恢复工作台 | 清理失败场景并重建库存 | 原有 `/produce_orders/recover`，有二次确认 |

A1–A4 表示逻辑箱位，不代表初始生成四只箱子。空位显示“空位 · 无箱”；一个混合订单对应一个箱子。未实现的“库存中心 / 任务调度”禁用并注明规划中，不显示虚构库存或队列。现有后端每订单箱最多四件，本次没有擅自改变物理装箱限制。

## 模块拆分

1. `produce_order_workstation/debug_panel.py`：保留 `RosWorker`。Qt 工作线程负责 ROS spin、ActionClient、两个服务客户端和状态订阅，队列接收界面命令、信号返回结果。`DebugPanel` 作为兼容入口继承新面板。退出时判断 `rclpy.ok()`，避免 ROS 已关停后重复 shutdown。
2. `produce_order_workstation/operator_panel.py`：`OperatorPanel` 管理页面布局和交互。订单草稿、正在执行的任务、服务器箱位三个状态独立；编辑下一单或收到每秒状态快照不会清除最近任务结果。重启面板会读取最新报告恢复历史结果，但不会假装重新接管正在执行的 Action。
3. `produce_order_workstation/panel_components.py`：`Card`、`Metric`、`ProduceGlyph`、`BinCard`、`StageStrip` 与统一 QSS。Qt 原生绘图代替下载图标，资源和状态文字以 PlainText 显示。

调用链未改：用户点击提交 → `worker.submit` 入队 → ROS `ExecuteOrder` → 原有工作站/Agent/运动执行 → feedback/result 信号 → 任务卡；取货/恢复仍调用原有服务。进度不使用定时器假增，`MOTION_STAGE.step` 的 grab/lift/place/retreat 对应当前物品阶段；完成数始终使用真实反馈。五个阶段不是整单固定执行五步，下一件商品会重新经历定位/抓取/装箱。

## 安全与交互修正

- 校验只更新草稿区域，不再把任务进度清零。
- 完成、失败、取消的结果一直保留到下一单提交；长错误折行，完整反馈在技术日志。
- 在线并收到有效工作台快照才启用提交、取货、恢复；连续五秒未收到状态则显示未同步，禁用这些操作。
- 取消只作用于本面板已经接收的 Action Goal，不假装能取消其他客户端订单。
- 不增加不存在的暂停、恢复执行服务；“恢复工作台”是重建场景，不是继续上次任务。
- 本次没有发起实际订单，没有调用场景恢复或人工取货；只重新打开订单面板，后台与仿真不重启。

## 验证

- `colcon build --packages-select produce_order_workstation --symlink-install` 成功。
- 原有后端测试 76 项通过。隔离 ROS 自动加载的 pytest 插件后运行，避免其特殊收集逻辑把普通单测跳过：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=ros2_ws/src/produce_order_workstation:$PYTHONPATH python -m pytest -c /dev/null --confcutdir=ros2_ws/src/produce_order_workstation/test -q ros2_ws/src/produce_order_workstation/test -p no:cacheprovider
```

- 系统 Python + 现有 PyQt5 单独运行 8 项界面回归：离线、数量校验、草稿与结果隔离、心跳保留结果、状态过期、运动阶段、提交锁定、报告/回执/重启显示、小窗口无横向页面滚动（部分断言合并在同一测试内）。Conda 无 PyQt5 时只跳过界面测试，不安装额外依赖：

```bash
source /opt/ros/jazzy/setup.bash
source ros2_ws/install/local_setup.bash
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -s ros2_ws/src/produce_order_workstation/test -p test_operator_panel.py -v
```

截图 `docs/images/order-workbench.png` 是离屏渲染的新面板，实时订阅当前工作台状态和读取真实历史报告，不是虚构订单执行演示。截图时后台上一次订单失败，需要用户确认恢复后才可再次下单。
