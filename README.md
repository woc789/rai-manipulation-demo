# ROS 2 + Agent 生鲜配货工作站

基于 [RobotecAI/rai-manipulation-demo](https://github.com/RobotecAI/rai-manipulation-demo) 和 [RAI](https://github.com/RobotecAI/rai) 的 **Fork 二次开发分支**，保留上游历史与许可，非 RobotecAI 官方版本。

当前版本：[woc789/rai-manipulation-demo · development](https://github.com/woc789/rai-manipulation-demo/tree/development)。

## 能做什么

在 O3DE 仿真中，用 Panda 机械臂完成混合生鲜订单：

- 四类商品视觉定位、抓取与装箱，一份订单对应一个箱子。
- 有限库存、库存预留、多订单排队、补货与取货。
- Agent 通过 MCP 获取业务上下文，生成受约束计划；行为树组织执行，MoveIt 规划运动。
- SQLite 保存订单与执行记录，Qt 界面展示状态和操作入口。

## 环境与安装

Ubuntu 24.04、ROS 2 Jazzy、Python 3.12、NVIDIA GPU、图形桌面；无需 Docker。模型使用 Conda `rai-jazzy`，界面使用系统 Python/PyQt5。

**首次使用：按 [安装与环境配置](docs/INSTALL.md) 的 A～G 顺序完成。** 文档包含源码下载、依赖、编译、仿真与视觉资源下载。视觉缓存约 2 GB，另需下载完整仿真包；运行订单需要自己的百炼 API Key 和已开通的模型。

| 配置 | 位置 / 用途 |
|---|---|
| 启动配置 | `~/.config/produce_order_workstation/launch.env`，模板：[launch.env.example](config/launch.env.example) |
| 必填路径 | `PRODUCE_RAI_ROOT` 指向外层 RAI；Conda、行为树路径按实际安装位置填写 |
| 模型配置 | `PRODUCE_AGENT_MODEL`、`PRODUCE_AGENT_BASE_URL`；代理按需设置，不照抄他人端口 |
| API Key | `~/.config/rai/bailian.env`，内容为 `DASHSCOPE_API_KEY=自己的密钥`，权限设为 600，禁止提交 Git |
| 数据库 | `PRODUCE_STATE_DB` 指定 SQLite 路径；换路径意味着换一份业务历史 |

现有环境不要重复安装或覆盖配置。修改启动配置后需要安全重启；`doctor` 不检查模型额度。

## 启动与使用

在已登录图形桌面的终端，进入项目目录：

```bash
cd ~/rai_embodied_project/src/examples/rai-manipulation-demo
bash start.sh doctor
bash start.sh start
```

自定义安装目录请替换第一行路径。脚本自动准备运行环境并打开仿真与订单界面，无需手动激活 Conda；不要再单独启动第二套仿真。

1. 等待工作台显示 **就绪 / READY**。
2. 填写商品数量（不需要的填 0，每单合计最多 8 件），点击 **提交订单**。
3. 在订单队列查看登记和执行状态；已登记不代表立即执行。
4. 配货完成后，选择对应订单，点击 **确认取货**，释放箱位。
5. 库存不足时使用 **视觉复查**；确认缺货且工作台允许操作后，使用 **补货准备**。

## 常用操作与异常

| 情况 | 操作 |
|---|---|
| 界面关闭了 | `bash start.sh panel`，仅重开界面 |
| 想查看运行状态 / 错误 | `bash start.sh status` / `bash start.sh logs-once` |
| 不需要某个等待订单 | 选中该单 → 取消选中排队单；批量用清除待执行队列 |
| 找不到商品 | 检查相机数据 → 视觉复查；不要直接重复下单 |
| 等待箱位 | 对已完成订单确认取货 |
| 失败、需要恢复或历史现场待核对 | 先核对现场，再按 [故障处理指南](docs/OPERATIONS.md#5-出问题时按状态处理) 操作；恢复可能清理现场，不要删库绕过 |
| 模型鉴权或额度错误 | 检查自己的 Key、模型、地区和额度；补货不能解决 |

停止前先处理当前动作及订单，再执行：

```bash
bash start.sh stop
# 修改配置后、确认现场安全再重启：
bash start.sh restart
```

关闭界面不等于停止后台；“取消”不是急停。完整按钮、重启确认和日志说明见 [运行指南](docs/OPERATIONS.md)。

## 文档与许可

[安装配置](docs/INSTALL.md) · [运行与恢复](docs/OPERATIONS.md) · [环境版本](environment/runtime-reference.json) · [RAI 补丁](patches/README.md) · [验收记录](docs/STARTUP_RELEASE_VALIDATION_2026-10-21.md)

本项目是单机仿真开发版，仍存在视觉漏检、遮挡及模拟器原生退出等已知限制，不保证订单全部成功。保留上游 [Apache-2.0 许可证](LICENSE.txt) 和素材 [NOTICE](Project/Assets/EdibleItems/NOTICE)；模型与仿真包从原渠道下载，不包含在仓库内。
