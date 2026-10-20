# 替换障碍物：模型已准备，运行时接入尚未完成

- 用户需求：不再使用周转箱，使用网上可获取的实际物品模型替换障碍物。
- 选择：Kenney City Kit (Roads) 2.2 的 `construction-cone`，作为桌面安全区域提示锥。
- 官方来源：https://kenney.nl/assets/city-kit-roads
- 授权：CC0，已保留原始授权文本。
- 下载：完整素材包约 2.7 MiB；项目仅保留该模型 FBX（26,816 字节）、GLB（7,056 字节）、调色板贴图和授权。
- 目录：`Project/Assets/ThirdParty/KenneyCityRoads/`。
- 原始 ZIP SHA-256：`374a4552115cbd11b4ec5c930ee31a533562dc78a09af249a655d03f7ea3b959`。

## 已确认的接入限制

- 当前使用预打包的 `RAIManipulationDemo.GameLauncher`，资源来自 `Cache/linux/engine.pak`。
- 场景生成由 `O3DExROS2Bridge._spawn_entity()` 调用 ROS SpawnEntity，名称对应已注册 spawnable。
- 本机检查的目录没有找到 O3DE 资源编译器或 Editor，也没有 Docker 可执行程序。
- 新 FBX/GLB 不能仅靠填写场景配置变成当前游戏可生成的实体；需要匹配的资源编译环境和产物注册。
- 官方说明：https://docs.o3de.org/docs/user-guide/assets/pipeline/asset-processing/
- 尚未安装新引擎或大型工具；不能在未核算下载量、版本兼容性前假定满足原先 200 MB 依赖下载限制。

## 接入后应做的工作（尚未执行）

1. 确认打包仿真的 O3DE 版本，评估兼容资源工具的下载量、磁盘与项目 Gem 依赖。
2. 编译模型、材质和独立静态障碍物 prefab/spawnable；不覆盖收货箱 prefab。
3. 设置合理的小型桌面比例，验证朝向、底面高度和模型实际包围尺寸。
4. 将原 crate 专用同步逻辑调整为新障碍物的实测位姿和对应碰撞几何，保留位置核验与碰撞检查。
5. 选择避开商品下降区域及箱口的位置；先做只规划、不执行的可达性与碰撞检查。
6. 测试真实相机四类检测，再测试抓取、转运及八件订单；不因更换障碍物擅自清除当前暂停单。

当前状态不是“已替换成功”。原仿真、4/8 暂停订单及箱子均未因本次准备工作修改。
