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

## 1 GB 下载限额核算（2026-10-20）

- 用户随后将允许的新下载总量提高到 1 GB。
- 从实际运行包 `Cache/linux/engine.pak` 只读提取 `engine.json`，得到内部引擎版本 `2.3.2`；它与 O3DE 官方 `2409.2` 标签的 `engine.json` 相同。
- 注意：项目源码 `Project/project.json` 声明 `4.2.0`，不能用此声明代替预打包仿真实际版本；若构建工具项目，还需解决这个版本差异及 Gem 依赖。
- 仅请求官方安装包 HTTP 响应头核算，没有下载安装包主体：
  - `https://o3debinaries.org/main/Latest/Linux/o3de_2409_2.deb`：`Content-Length: 5911401260`，约 **5.91 GB**（十进制）。
  - `https://o3debinaries.org/main/Latest/Linux/o3de_2510_0.deb`：`Content-Length: 2873067256`，约 **2.87 GB**；不作为当前旧运行包的兼容替代保证。
- 官方内部版本依据：https://raw.githubusercontent.com/o3de/o3de/2409.2/engine.json
- 两者安装包本身均已超过 1 GB；安装后额外依赖与解压空间尚未计入，不承诺 6 GB 能覆盖完整接入。
- 因超过用户限额，停止下载/安装大型工具，不修改现有场景或暂停订单。所选安全锥源素材仍已保存在项目中，尚未完成运行时导入。
