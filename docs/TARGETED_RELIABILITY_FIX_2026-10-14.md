# 定向功能测试问题修复（2026-10-14）

## 范围与基线

本次仅修复上一次针对性测试复现的五类问题，不新增业务阶段。修改前提交为 `7a20f32`。原第 4–7 步备份及用户订单数据库不删除、不重置。未安装新依赖。

之前 18 项定向探针中，13 项通过、5 项失败。探针使用真实库存账本与临时 SQLite 数据库，机械技能及云模型为替身；时钟 QoS 检查使用真实 ROS 传输。并发问题证明的是两个入口可能同时调用技能，不等同于已观察到实体机械臂双重运动。

## 1. 订单批准与库存预留的原子性

**之前：** 批准修改订单时，先释放库存并修改内存，再保存 SQLite。写盘异常后，内存数量与数据库数量不同，请求仍然待批准。

**现在：** `BusinessCoordinator.call()` 在管理器锁和 `InventoryLedger.transaction()` 内完成订单变更和数据库提交。保存失败时恢复订单对象、数量、队列状态、原因、缺货信息、扫描请求标志及库存预留；保存成功才对其他线程可见。数据库中的请求批准状态仍与快照在同一 SQLite 事务提交。

**上下游：** 操作员批准 → 业务协调器 → 订单队列/库存账本 → SQLite。输出是全部成功或全部回滚，不是部分修改。

**技术：** 互斥锁、可重入锁、内存回滚、数据库事务、资源一致性。

## 2. 区分维护批准与实际执行结果

**之前：** 补货/恢复请求先标记已批准，执行入口随后拒绝，操作员不能再次使用原请求；批准也不能表达维护是否完成。

**现在：** 请求保留 `PENDING/APPROVED/REJECTED` 的批准状态，额外记录执行状态：

```text
批准 → ADMITTING → RUNNING → SUCCEEDED
             ↘ REJECTED（确定未启动，可再次确认）
                        ↘ FAILED（执行失败，需核对）
                        ↘ UNKNOWN（结果未知，禁止盲目重放）
```

`WorkcellStore.admit_maintenance()` 持久化准入；`OrderActionServer.start_maintenance()` 校验该准入，再启动维护线程。线程结束后记录实际结果。更新使用状态条件，快速完成的 `SUCCEEDED` 不会被迟到的 `RUNNING` 覆盖。

只有明确未启动的 `REJECTED` 可以在原请求有效期内重新确认。运行中、执行失败或结果未知不得自动重试。进程重启时，残留 `ADMITTING/RUNNING` 变为 `UNKNOWN`，工作站进入人工核对状态。

恢复旧现场的人工核对标志只在场景真正重建成功后清除；失败时保留。历史订单的已完成数量保留，丢弃旧现场不算已交付。现有“先取走待取货箱”的保护没有绕过。

界面请求列表显示“执行中”“未启动，可重试”“结果未知，需核对”等状态；可重试请求仍需操作员再次确认。错误详情可查看请求行提示。

**技术：** 两层状态机、持久化准入、异步线程、条件更新、人工确认与安全恢复。

## 3. 已验证抓放结果不能因反馈丢失而再次执行

**之前：** 抓放已成功，操作日志也已写入，但随后快照写入失败；重试只得到“操作编号已存在”，无法查询原来的成功结果。

**现在：** `ExecutionBridge` 将完整步骤返回值、请求候选 ID、各 SKU 已完成数量写入操作日志；先记住返回值，再写事件和快照。重复相同请求直接返回已记录结果，不再次抓取。内存缓存丢失但操作日志还在时也能恢复结果。

若操作结果日志本身写入失败，已有 `INTENT` 不会被当成成功，也不会重新运动；工作站标记物理状态不确定并要求核对。重启恢复还会用 `VERIFIED` 操作日志补足落后的订单快照进度，但仍核对现场，不恢复旧坐标或自动重放任务。

**技术：** 幂等请求、操作 ID、执行意图日志、结果缓存、检查点恢复。它提供防重放与可追溯性，不声称数据库能单独保证物理动作的“恰好一次”。

## 4. MCP 与行为树共用一个技能执行门

**之前：** MCP 入口与 BT 入口各用自己的锁，不能阻止同一工作站同时进入两个技能调用。

**现在：** 新增 `ExecutionGate`，按工作站对象共享。MCP 调用、BT 抓放/校验及工作站感知/运动/抓放/结束技能使用同一可重入锁。同线程嵌套合法，其他线程并发进入会明确拒绝，不在后台悄悄等待后再执行。

正常 BT 订单的 MCP 会话只允许感知，不允许直接调用抓放或结束工具；物理步骤由行为树推进。旧版显式 Agent 执行模式保留原工具能力，但同样经过共享执行门。资源读取不变，Agent 仍通过 MCP 获取视觉候选。

**技术：** 工作站级互斥、可重入锁、装饰器、能力限制、执行入口统一。

## 5. 仿真时钟 QoS 与失联监测

**之前：** `/clock` 订阅默认 Reliable，无法接收 Best Effort 发布端；一直没收到时钟时健康检查反而直接返回。

**现在：** 生产订阅使用 `CLOCK_QOS`（Best Effort，深度 1），兼容这类时钟发布端。分别记录消息到达时间和仿真时间推进时间，检测从未收到、消息中断、持续收到但时间冻结三种情况。

首次等待宽限默认 240 秒，可通过配置 `clock_health.startup_grace_s` 设置；中断或冻结阈值为 15 秒，使用单调壁钟而非仿真时钟计时。检测异常后禁止继续运动，正在执行的订单进入核对流程。时钟恢复不能自动解除已隔离物理任务。就绪状态更新也会检查时钟异常，避免初始化/维护迟到结果覆盖失联状态。

永久 QoS 回归测试直接使用生产 `CLOCK_QOS`。原故障探针硬编码 Reliable 的订阅不是生产代码，不能仅修生产代码就让它收到 Best Effort 消息，因此回归测试改为引用实际生产配置，而不是放宽收包断言。

**技术：** ROS 2 QoS 兼容性、Best Effort、单调时钟、健康监测、故障隔离。

## 文件定位

路径均相对于本仓库的 `ros2_ws/src/produce_order_workstation/`：

| 功能 | 文件 |
|---|---|
| 订单变更原子性、维护批准 | `produce_order_workstation/business.py` |
| 库存回滚事务 | `produce_order_workstation/inventory.py` |
| 维护执行状态、操作结果查询 | `produce_order_workstation/persistence.py` |
| 抓放步骤幂等、操作结果恢复 | `produce_order_workstation/execution_bridge.py` |
| 共享执行门 | `produce_order_workstation/execution_gate.py` |
| 技能入口互斥、BT 会话权限 | `produce_order_workstation/runtime.py`、`mcp_server.py` |
| 维护线程、重启进度、时钟健康 | `produce_order_workstation/order_server.py` |
| 维护请求状态与再次确认 | `produce_order_workstation/operator_panel.py` |
| 定向回归用例 | `test/test_targeted_reliability.py` |
| 界面回归用例 | `test/test_operator_panel.py` |

## 验证与边界

验证结果：

| 检查 | 结果 | 边界 |
|---|---|---|
| 工作站非 Qt 回归 | 262 通过、8 跳过 | 跳过项包含另行启用的 ROS 集成等用例；有 2 条第三方依赖弃用警告 |
| 本次定向回归 | 33 通过 | 已包含在上述 262 项内，不重复计数 |
| ROS/MCP/BT/队列集成 | 7 通过 | 真实 ROS Service/Action/MCP HTTP、已编译 BT；模型与机械执行为测试端，不是真实仿真运动 |
| Qt 操作面板 | 41 通过 | 离屏测试，含维护可重试与重复确认保护 |
| `colcon build --symlink-install --packages-select produce_order_workstation` | 构建成功 | 使用现有 Jazzy 与独立 Conda 环境；接口及 C++ 本次未改 |
| `git diff --check` | 通过 | 无空白补丁错误 |

测试均使用临时数据和隔离 ROS domain（通信集成 71、界面 72、定向 73、最终回归 74），不读取/重置用户业务数据库，不调用收费模型，不替用户下真实订单。

复测入口：在 `/home/wang/rai_embodied_project` 中使用现有脚本
`src/examples/rai-manipulation-demo/ros2_ws/src/produce_order_workstation/scripts/check_phase47.sh unit`
或 `integration`。Qt 测试需使用系统 `/usr/bin/python3` 和 `QT_QPA_PLATFORM=offscreen`，不能用缺少 PyQt5 的 Conda Python。`.venv` 目前不存在，实际采用既有 `rai-jazzy` Conda 环境，不为此次测试新装环境。

本次验证针对逻辑、ROS/MCP 通信和界面，不代表真实视觉漏检率、完整仿真运动质量或长期连续运行均已解决。未启动完整仿真；常驻进程须重启后加载修改。
