# 百炼 Agent 模型切换（2026-10-09）

- 原模型：`qwen3.8-flash`。实际订单返回 403 / `AllocationQuota.FreeTierOnly`，免费额度耗尽。
- 新模型：`qwen3.7-flash-2026-07-15`。用户当前额度截图显示剩余 1M tokens、到期 2026-10-23，并开启用完即停。
- 保持原北京 OpenAI-compatible endpoint 与用户私有 Key 不变；不关闭用完即停，不自动回退到其他付费模型。
- 修改 `config/workstation.yaml` 中 `agent.model`，同步 README 和配置回归测试。
- 使用项目现有 `create_model` 和 `TOOLS` 做一次真实调用，返回相同模型 ID 和 `get_object_positions({"sku":"corn"})`，消耗 628 tokens。
- 测试未执行返回的工具，未提交订单，未控制机械臂。模型只更换 Agent 决策端，Grounding DINO / 分割 / RGB-D 定位保持不变。
- 在无执行订单、无排队订单和无待取货箱的状态下重启工作台与面板，以加载新配置。内存失败记录清空，磁盘历史报告保留。
- 仍支持 `PRODUCE_AGENT_MODEL` 环境变量覆盖默认配置。
