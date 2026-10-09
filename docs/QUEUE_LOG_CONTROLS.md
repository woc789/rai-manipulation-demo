# 队列与日志清理按钮

日期：2026-10-09。

## 界面入口

- “任务调度”页操作栏：**清除待执行队列**，点击后需确认。
- “事件”或“技术日志”页签右上角：**清空日志**，同时清空这两块文本显示。

## 清队列的边界与通信

OperatorPanel 根据最近的订单状态快照挑选 QUEUED、WAITING_STOCK、WAITING_BIN 且完成数量为零的订单。
确认后通过 Qt → RosWorker 的线程命令队列传递订单 ID 快照。
RosWorker 复用 `/produce_orders/cancel_queued` 的 CancelQueuedOrder Service，
每个请求明确 `discard_partial=False`，不引入绕过服务端检查的新路径。

服务端 `OrderQueue.cancel()` 在互斥保护下逐单确认是否尚未开始，并释放其未执行的库存预留。
正在执行、正在调度、执行结果不明、暂停部分单、已完成待取货的订单不属于批量清理目标。
如果确认后某份订单已开始，服务端拒绝取消，界面报告跳过，不能把它伪装成已清除。

这是批量独立取消，不是全队列事务。不影响机械臂正在执行的订单，不删除箱子。
新登记的订单不在确认时的快照中，需另一次清理。
请求统一监测 10 s 响应时限，失联不会让按钮无限等待；迟到回调不重复统计。
超时订单只是未确认取消，应以最新状态 Topic 为准，不自动重发。
取消成功的订单从任务调度表隐藏，但服务端状态记录保留，不伪装成从未提交过。

## 清日志的边界

只调用事件框和技术日志框的 `clear()`，不删除订单 JSON 报告、取货凭据或 systemd 日志。
不重置订单状态、进度、库存或 worker 的事件计数。
后续新事件仍正常显示；不做暂停日志或永久屏蔽。

## 代码入口

- `operator_panel.py`：`_clearable_orders()`、`_clear_queue()`、`_on_queue_cleared()`、`_clear_logs()`。
- `debug_panel.py`：`clear_queue()`、`_clear_queue()`、`_finish_queue_clear_entry()`、`_check_clear_queue_timeout()`。
- 服务端复用 `order_server.py.cancel_queued_callback()` 和 `scheduling.py.OrderQueue.cancel()`，无需重启服务端。

测试只使用离屏 Qt 和模拟 Service Future，不清除用户的真实订单。
离屏界面及模拟服务工作线程测试共 21 项通过，包括确认取消、状态竞争、超时和迟到结果。
部署只重启界面进程，保留正在运行的工作站、机械臂及内存中的订单/箱位状态。
