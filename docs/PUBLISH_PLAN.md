# 开发版发布计划与实际边界

## 发布目标

- 名称：ROS 2 + Agent 生鲜配货仿真工作站。
- 定位：基于 RobotecAI RAI 与 rai-manipulation-demo 的二次开发、单机 Ubuntu 24.04/Jazzy 仿真开发版。
- 发布形式：Fork `RobotecAI/rai-manipulation-demo`，在 Fork 内发布改进分支，保留上游提交历史、LICENSE 和 NOTICE；不是新建空仓库重新声明独立原创项目。
- 不宣称五轮压力测试通过，不宣称真机/工业可靠性或端到端 VLA。
- 不增加功能键，不启动新压力批次，不自动安装/下载大型资源。

## 本轮执行项目

1. 启动配置从硬编码迁到外部 `launch.env`；终端显式设置优先，密钥不进入仓库或 systemd Environment。
2. 本机外部配置保留当前数据库与必要代理，不删除旧历史，不悄悄切到新库。
3. 统一 runtime 环境解析；移除默认 GDB，使用绝对解析路径，启动先检查依赖，再等待真实 ROS 就绪。
4. 加入 doctor/ready/status/logs-once，错误会给出下一步，不把后台 active 当业务成功。
5. 重写 README 的首次启动、下单、排队、取货、补货、取消、恢复、停止流程；保留完整首次安装文档。
6. 固定 RAI 基线并携带六处必要源码补丁；提供版本/外部资产指纹，不上传模型和模拟器。
7. 添加敏感文件忽略规则、只读发布审计和轻量 CI；审计当前工作树及 Git 历史，结果不打印秘密值。
8. 收拢此前已实施的改动、验证后本地 Git 保存；不改 origin、不创建远程仓库、不 push。

## 仍需人工验收 / 不能自动声称完成

- 用户按 README 在本机从停止状态启动、确认恢复入口与实际使用体验。
- 按用户要求，不额外安装/验证新环境；README 直接提供与本机对应的依赖、资源下载、配置和启动流程，明确驱动/硬件适配边界，不宣称跨机器保证。
- 模型账户地区、型号、余额与免费额度由账户决定；启动检查不会发起计费模型请求。
- 原生模拟器退出问题尚无根因结论；正常启动不再默认附加调试器。
- 玉米漏检与箱子遮挡仍在已知问题中。固定压力批次第三轮结束后，第四轮受控失败；没有第五轮通过记录。
- 旧技术文档是历史阶段记录，可能仍出现已移除对象名；README/当前代码优先，不把它们全当最新架构。
- 素材许可证/上游 NOTICE 保留；发布者仍需核对新增素材与二进制的分发条件，不把可下载等同于可重新授权分发。

## 发布前检查

本机启动、界面重开与幂等启动已验证；单元回归 420 通过、9 跳过，Qt 回归 46 通过，发布审计测试 3 通过。具体操作与未覆盖范围见 [启动与发布准备验收](STARTUP_RELEASE_VALIDATION_2026-10-21.md)。这些是已有本机结果，不构成跨硬件安装保证；按用户要求不新增新机安装验证。

```bash
python3 scripts/release_audit.py --history
bash start.sh doctor
bash start.sh ready
git diff --check
git status --short
```

审计是启发式，不能保证识别所有秘密；若发现真实泄漏，先撤销/轮换密钥，再协商历史清理，不能直接强推改写历史。

## 维护者：按 Fork 关系发布，不重建空仓库

当前已确认 Fork 为 `https://github.com/woc789/rai-manipulation-demo`，目标分支为 `development`。本地已将该 Fork 设置为 origin，RobotecAI 原仓库设置为 upstream。下面步骤保留作维护说明；不要对已经完成设置的目录重复添加远程。发布成功与否以实际推送及远程检查结果为准。

1. 登录自己的 GitHub，在 [RobotecAI/rai-manipulation-demo](https://github.com/RobotecAI/rai-manipulation-demo) 点 **Fork**，创建自己的派生仓库。GitHub Fork 关系是平台操作，本地 `git branch` 或给新仓库改名不能代替。
2. 提供该 Fork 的实际 HTTPS/SSH 地址。当前本地分支为 `kd/vacuum_gripper`，包含上游历史和本项目后续提交，不需要重新 `git init`、复制文件或删除历史。
3. 在当前项目根目录，确认远程仍为记录的状态后，将原 origin 保留为 upstream，将 origin 指向自己的 Fork。以下是待确认地址后的维护者命令，**本轮没有执行**：

```bash
git remote -v
# 仅在 origin 仍为 RobotecAI、且没有同名 upstream 时：
git remote rename origin upstream
git remote add origin https://github.com/woc789/rai-manipulation-demo.git
# 当前已提交 HEAD 推送为 Fork 的专用分支，不覆盖上游分支：
git push origin HEAD:development
```

4. README 已填写实际账号 `woc789` 与目标分支 `development`。仓库介绍应明确链接此分支，否则访客可能看到其他分支；不要未经确认改变默认分支设置。
5. 仓库首页保留 `forked from RobotecAI/rai-manipulation-demo` 的关系，README 保留上游来源和改动范围；不要求将此业务分支合并回上游才能展示。

不向 RobotecAI 上游 origin 发布推送，不使用 `git push --mirror`、强推、历史重写或独立新许可证覆盖上游许可。大型模型/仿真包使用原始获取说明，不提交业务数据库。Fork 地址未提供前保持本地远程配置不变。

## 使用者的目标路径

README 的 A～G：准备系统 → 克隆 Fork 发布分支与固定 RAI → ROS/BT 依赖 → Conda 与编译 → 仿真/视觉资源下载 → 外部配置和自己的 Key → doctor/start → READY 后提交订单。既不需要运行压力测试，也不需要先构建维护者的旧订单数据库。

参考：[GitHub Fork 操作](https://docs.github.com/en/pull-requests/how-tos/work-with-forks/fork-a-repo?tool=webui)、[配置 upstream](https://docs.github.com/en/pull-requests/how-tos/work-with-forks/configuring-a-remote-repository-for-a-fork)、[敏感信息处理](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)、[大文件规则](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。
