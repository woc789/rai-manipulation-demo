# RAI 兼容补丁

上游：RobotecAI/rai，Apache-2.0；保留上游版权、许可证与第三方 NOTICE。

`rai-workcell.patch` 以 `6802d4073e8caa2ab72c5509fa1eeeb659663f64` 为基线，提取本机已使用的六个源文件修改：

- 检测与分割模型顺序初始化；
- 图像编码归一化、ROI 坐标映射与检测合并；
- 配对 RGB-D 时间戳/深度注册、可取消服务等待和连接复用；
- 场景服务无响应与超时诊断。

不包含模型权重、API Key、Conda 环境、RAI 的完整副本或本机文档。

在干净的、固定基线 RAI 克隆中执行（路径换成自己的）：

```bash
git -C /path/to/rai apply --check /path/to/this-project/patches/rai-workcell.patch
git -C /path/to/rai apply /path/to/this-project/patches/rai-workcell.patch
```

已应用时只检查，不重复应用：

```bash
git -C /path/to/rai apply --reverse --check /path/to/this-project/patches/rai-workcell.patch
```

本机 RAI 已经包含这些修改；不要对现有工作目录 checkout/reset。首次安装流程见 `docs/INSTALL.md`。未来若换用其他 RAI 版本，需要重新适配，不保证补丁可直接应用。
