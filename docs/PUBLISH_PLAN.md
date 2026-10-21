# 开发版发布计划与实际边界

## 发布目标

- 名称：ROS 2 + Agent 生鲜配货仿真工作站。
- 定位：基于 RobotecAI RAI 与 rai-manipulation-demo 的二次开发、单机 Ubuntu 24.04/Jazzy 仿真开发版。
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
- 全新电脑的系统依赖、Conda/CUDA、O3DE 二进制与视觉缓存准备尚未在另一台机器验收；doctor 不代替这项工作。
- 模型账户地区、型号、余额与免费额度由账户决定；启动检查不会发起计费模型请求。
- 原生模拟器退出问题尚无根因结论；正常启动不再默认附加调试器。
- 玉米漏检与箱子遮挡仍在已知问题中。固定压力批次第三轮结束后，第四轮受控失败；没有第五轮通过记录。
- 旧技术文档是历史阶段记录，可能仍出现已移除对象名；README/当前代码优先，不把它们全当最新架构。
- 素材许可证/上游 NOTICE 保留；发布者仍需核对新增素材与二进制的分发条件，不把可下载等同于可重新授权分发。

## 发布前检查

本机启动、界面重开与幂等启动已验证；单元回归 420 通过、9 跳过，Qt 回归 46 通过，发布审计测试 3 通过。具体操作与未覆盖范围见 [启动与发布准备验收](STARTUP_RELEASE_VALIDATION_2026-10-21.md)。这些结果不替代用户验收或新机安装验收。

```bash
python3 scripts/release_audit.py --history
bash start.sh doctor
bash start.sh ready
git diff --check
git status --short
```

审计是启发式，不能保证识别所有秘密；若发现真实泄漏，先撤销/轮换密钥，再协商历史清理，不能直接强推改写历史。

## 确定远程地址之后再做

- 用户提供/确认自己的 GitHub 仓库地址、可见性与发布版本。
- 在验收通过后再建立远程与 push；origin 当前属于 RobotecAI 上游，不对它执行发布推送。
- 不执行 `git push --mirror`、强推或自动重写旧历史。
- 大型模型/仿真包使用原始获取说明或合规 Release 分发，不提交业务数据库。

参考：[GitHub 敏感信息处理](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)、[大文件规则](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。
