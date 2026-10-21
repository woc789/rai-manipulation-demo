# 首次安装与依赖准备

本机已经有环境：**不要执行本页重装**，回到 README 的 doctor/start。[README](../README.md) 的 A～G 是完整操作顺序，本页补充环境细节。按本机已有可运行环境整理依赖，不额外进行新环境安装验证；硬件与驱动差异需使用者自行适配。本仓库按 `RobotecAI/rai-manipulation-demo` 的 Fork / 二次开发分支发布，保留上游历史和许可。

## 1. 环境边界

- Ubuntu 24.04、ROS 2 Jazzy、Python 3.12、图形桌面、用户级 systemd。
- 需要兼容的 NVIDIA/CUDA/PyTorch 环境；本机参考为 RTX A4000、16 GB 系统内存与 16 GB 显存。不是最低配置保证。
- 不要求 Docker。不混入其他 ROS 发行版的 AMENT/PYTHONPATH/动态库。
- Python 模型使用独立 Conda `rai-jazzy`；Qt 与 ROS 命令使用系统 Python。
- RAI 源码、RAI ROS overlay、本项目 ROS overlay、O3DE 完整运行包、三种视觉缓存、外部大模型账户缺一不可。

参考版本和资产 SHA256 在 [environment/runtime-reference.json](../environment/runtime-reference.json)。这是已观测版本，不是完整依赖锁。

### 本机已经安装了什么、装在哪里

以下是当前电脑的对应关系；`~` 指登录用户家目录，新电脑要替换成自己的路径。

| 层次 | 本机位置 / 环境 | 作用 |
|---|---|---|
| 系统 ROS 2 | `/opt/ros/jazzy` | ROS 通信、Action、消息与系统工具；不是 Conda 中安装的 ROS |
| Python / 模型环境 | `~/miniconda3/envs/rai-jazzy` | Python 3.12、PyTorch、检测/分割、RAI Python 包、MCP/OpenAI SDK |
| 系统 Qt | 系统 `/usr/bin/python3` 的 PyQt5 | 订单窗口；与模型 Conda 环境分开，避免 Qt 插件冲突 |
| 外层 RAI 源码和构建结果 | `~/rai_embodied_project/src/`、`~/rai_embodied_project/install/` | 视觉、仿真桥、RAI 接口与 bringup；Python 包使用可编辑安装关联源码 |
| 本项目源码和构建结果 | `~/rai_embodied_project/src/examples/rai-manipulation-demo/ros2_ws/src/`、对应的 `ros2_ws/install/` | 工作站、Agent、行为树、自定义消息、机械臂 C++ 执行节点 |
| BehaviorTree.CPP | `~/.local/share/rai-deps/behaviortree-jazzy/prefix/opt/ros/jazzy` | C++ 行为树运行库；构建与启动须指向同一前缀 |
| MoveIt / ROS 控制依赖 | Jazzy 系统包及 RAI Panda 配置 | 碰撞检查、运动规划、轨迹执行；不装进 Conda 替代系统 ROS |
| O3DE 完整仿真包 | `~/rai_embodied_project/demo_assets/manipulation/RAIManipulationDemo/` | GameLauncher、Cache、动态库；不是 Python 包 |
| 检测 / 分割权重 | `~/.cache/rai/vision/weights/` | GroundingDINO 与 SAM2；运行时读本地权重 |
| Hugging Face 缓存 | `~/.cache/huggingface/` | BERT tokenizer 和模型缓存，供检测模型使用 |
| 本机启动配置 | `~/.config/produce_order_workstation/launch.env` | 路径、数据库、模型 Code、代理、ROS 域；不存 API Key |
| 私有模型凭据 | `~/.config/rai/bailian.env` | Agent 调用百炼；权限 600，不提交 Git |
| 业务数据 | `~/.local/share/produce_order_workstation/` | SQLite、报告、视觉诊断；不是 Git 源码，也不能通过删库随意“恢复” |

当前启动器依次加载 **系统 Jazzy → 外层 RAI overlay → 本项目 overlay**。overlay 是工作区编译后的环境层，让 ROS 能发现本项目的包和接口；随后仅在需要模型的进程中激活 Conda。界面仍使用系统 Python。启动器会清理旧发行版的路径，避免混入 Kilted 等环境。

日常启动不需要手动逐层 source，也不需要先激活 Conda；`doctor` 会输出实际路径。只有首次安装、重新编译或单独开发节点时，才需要按下面顺序手动准备环境。本轮整理只修改启动脚本、配置与文档，没有重新安装系统包、Conda 或下载模型。

## 2. 两个源码目录，不要直接重置现有目录

推荐将本 Fork 的发布分支放到外层 RAI 的 `src/examples/rai-manipulation-demo/`。以下变量必须改成**自己的真实绝对路径**；本 Fork 地址和发布分支从 GitHub 页面复制，不使用 RobotecAI 上游默认分支代替改进版。这里的两个源码目录不表示发布为两个独立原创项目：外层 RAI 是运行依赖，本仓库是 demo 的衍生分支。

```bash
export RAI_ROOT=/absolute/path/to/rai
export WORKSTATION=/absolute/path/to/this-project
```

在一个**新建、干净的 RAI 克隆**中准备固定基线：

```bash
git clone https://github.com/RobotecAI/rai.git "$RAI_ROOT"
git -C "$RAI_ROOT" checkout 6802d4073e8caa2ab72c5509fa1eeeb659663f64
git -C "$RAI_ROOT" apply --check "$WORKSTATION/patches/rai-workcell.patch"
git -C "$RAI_ROOT" apply "$WORKSTATION/patches/rai-workcell.patch"
```

本机已经有这些修改，**不要再 checkout，也不要重复打补丁**。补丁含义和已应用检查见 [patches/README.md](../patches/README.md)。不使用未发布的本机 RAI 提交号作为新电脑必须能下载到的版本。

## 3. 系统 ROS 与构建依赖

按 [ROS Jazzy 官方安装](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html) 完成系统 ROS，安装 Git LFS、colcon、vcstool、rosdep 和系统 Qt。下列 apt 操作只适用于配置好 ROS 源的 Ubuntu 24.04：

```bash
sudo apt-get update
sudo apt-get install git-lfs python3-colcon-common-extensions python3-vcstool python3-rosdep python3-pyqt5
git lfs install
git -C "$WORKSTATION" lfs pull
source /opt/ros/jazzy/setup.bash
cd "$RAI_ROOT"
vcs import src < ros_deps.repos
```

此基线的 repos 文件会导入 `src/src/rai_interfaces`。rosdep 若从未初始化，先按其提示执行初始化；已经初始化的不重复覆盖。

```bash
rosdep update
rosdep install --from-paths "$RAI_ROOT/src/src/rai_interfaces" "$RAI_ROOT/src/rai_bringup" "$WORKSTATION/ros2_ws/src" --ignore-src -r -y
```

检查输出并解决未解析项，不能因为 `-r` 继续执行就当所有依赖成功。特别需要 MoveIt/Panda 配置、gazebo_msgs（O3DE 服务接口）、depth_image_proc，以及 BehaviorTree.CPP 4.x；后者不能用不兼容的 3.x 代替。基线 RAI 的接口构建需要兼容 C++ 工具链，具体以其构建文档及报错为准。

本机 BehaviorTree.CPP 为 4.10.0。按 README 的 C 步下载 Jazzy deb 并解包到独立前缀，保存其真实路径，后续同时用于 CMAKE_PREFIX_PATH 与运行库路径。本机已有此前准备的前缀，不需再下载或复制覆盖。

## 4. 独立 Conda 与 Python 包

安装 Miniconda 后，使用自己的初始化脚本路径：

```bash
source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda create -n rai-jazzy python=3.12 pip -y
conda activate rai-jazzy
python -m pip install numpy==1.26.4 torch==2.3.1 torchvision==0.18.1 transformers==4.57.6 scikit-learn==1.3.2 rf-groundingdino==0.2.0 rai-gsam2==1.1.3
python -m pip install -e "$RAI_ROOT/src/rai_core" -e "$RAI_ROOT/src/rai_sim" -e "$RAI_ROOT/src/rai_extensions/rai_perception" -r "$WORKSTATION/requirements/runtime-extra.txt"
conda deactivate
```

CUDA、驱动与 PyTorch 分发源需要适配实际机器；不要把系统 ROS 包安装进另一个不兼容的 Python 版本。此页没有自动下载或修改本机环境；后续编译在退出 Conda 后使用系统 Python。

## 5. 构建两层 ROS overlay

在同一个 Jazzy 环境构建，按固定基线 RAI 的说明补齐其系统依赖：

```bash
source /opt/ros/jazzy/setup.bash
cd "$RAI_ROOT"
colcon build --symlink-install --packages-select rai_interfaces rai_bringup
source "$RAI_ROOT/install/local_setup.bash"
cd "$WORKSTATION/ros2_ws"
colcon build --symlink-install
source "$WORKSTATION/ros2_ws/install/local_setup.bash"
```

独立 BehaviorTree.CPP 前缀应在构建前加入 `CMAKE_PREFIX_PATH`；启动时用配置中的 PRODUCE_BT_PREFIX 指向同一前缀。不要同时 source 不同发行版/其他工作区的旧 `setup.bash`。当前启动器用 local_setup，避免回放另一台机器的旧链式 overlay。

## 6. O3DE 与视觉资源（新机才准备）

不需要从源码编译整个 O3DE。基线 RAI 提供 Jazzy/Ubuntu 24.04 的完整运行包：

```bash
cd "$RAI_ROOT"
source /opt/ros/jazzy/setup.bash
bash scripts/download_demo.sh manipulation
```

保留发布包中的可执行文件、Cache、依赖库；不要只复制一个 GameLauncher。真实文件路径填入启动配置，资产来源和当前可执行文件指纹见参考 manifest。下载包若更新，指纹可能变化，需重新验收，不伪称它仍是本机验证的同一版本。

视觉资源在本机已缓存。新机需要显式准备（下载量不是几十 MB，两个视觉权重约 1.6 GB，BERT 还约 0.44 GB；不包括仿真包）：

```bash
conda activate rai-jazzy
source /opt/ros/jazzy/setup.bash
source "$RAI_ROOT/install/local_setup.bash"
download-perception-models
python -c 'from transformers import AutoTokenizer; AutoTokenizer.from_pretrained("bert-base-uncased")'
python -c 'from huggingface_hub import hf_hub_download; hf_hub_download("bert-base-uncased", "model.safetensors")'
```

检查下载结果和 SHA256，不只相信下载程序退出码；doctor 会检查本地文件和缓存。正式启动离线使用视觉缓存，不自动修补一个下载到一半的权重文件。

## 7. 外部配置与 Key

```bash
mkdir -p "$HOME/.config/produce_order_workstation"
cp "$WORKSTATION/config/launch.env.example" "$HOME/.config/produce_order_workstation/launch.env"
chmod 600 "$HOME/.config/produce_order_workstation/launch.env"
```

用编辑器修改该文件中的 RAI 路径、Conda、可选 BT/模拟器路径、数据库与模型。它是可信 Bash 配置，**只使用自己编写的文件**。可用 PRODUCE_LAUNCH_CONFIG 指定另一份可信配置。

先 `mkdir -p ~/.config/rai`，再用编辑器将 Key 单独保存到 `~/.config/rai/bailian.env`，内容格式：

```dotenv
DASHSCOPE_API_KEY=YOUR_PRIVATE_KEY
```

随后 `chmod 600 ~/.config/rai/bailian.env`。不要把实际 Key 写进终端命令历史、README、项目配置、截图或 Git。systemd 启动器不转发环境中的 DASHSCOPE_API_KEY，而由 Agent 读取这个私有文件。

- PRODUCE_AGENT_MODEL：账户已开通且支持工具调用的真实模型 Code；没有保证免费的型号。
- PRODUCE_AGENT_BASE_URL：与账户地区匹配的兼容接口。
- PRODUCE_AGENT_PROXY_URL：只用于远程模型；不需要时留空。ROS/MCP 保持本机直连。
- PRODUCE_STATE_DB：绝对路径。新文件表示新的业务历史，不是旧订单恢复；长期修改请编辑配置，不只临时设置一次。
- PRODUCE_ROS_DOMAIN_ID：工作站、界面与外部 ROS 客户端必须一致。

## 8. 最后检查

```bash
cd "$WORKSTATION"
bash start.sh doctor
bash start.sh start
```

模型账户可用性仍由第一次真实调用验证。启动、下单、取货、补货及故障按钮的完整操作回到 [README](../README.md)。
