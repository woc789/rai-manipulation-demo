# 安装与环境配置

### 环境组成与本机安装位置

本机使用 **Ubuntu 24.04 + ROS 2 Jazzy + Python 3.12**，模型运行在独立 Conda `rai-jazzy`，界面使用系统 Python/PyQt5，**不需要 Docker**。本机参考硬件为 RTX A4000、16 GB 显存、16 GB 系统内存；这不是最低配置承诺。

| 组件 | 本机位置 | 用途 |
|---|---|---|
| ROS 2 Jazzy / MoveIt 等系统依赖 | `/opt/ros/jazzy` | 节点通信、消息、Action、运动规划和执行 |
| Conda 环境 | `~/miniconda3/envs/rai-jazzy` | PyTorch、GroundingDINO/SAM2、RAI Python 包、MCP/OpenAI SDK |
| PyQt5 | 系统 `/usr/bin/python3` 环境 | 订单界面，避免与模型环境的 Qt 插件冲突 |
| 外层 RAI | `~/rai_embodied_project/src/`、`~/rai_embodied_project/install/` | 视觉服务、仿真桥、接口和启动组件 |
| 本项目 | `~/rai_embodied_project/src/examples/rai-manipulation-demo/` | 工作站、Agent、库存/调度/数据库和 C++ 执行模块 |
| 本项目 ROS 构建结果 | 本项目 `ros2_ws/install/` | ROS 发现本项目节点与自定义接口所需的环境 |
| BehaviorTree.CPP | `~/.local/share/rai-deps/behaviortree-jazzy/prefix/opt/ros/jazzy` | C++ 行为树运行库 |
| O3DE 仿真运行包 | `~/rai_embodied_project/demo_assets/manipulation/RAIManipulationDemo/` | GameLauncher、Cache 与运行库；不能只保留可执行文件 |
| 检测 / 分割权重 | `~/.cache/rai/vision/weights/` | GroundingDINO 与 SAM2 本地权重 |
| BERT 模型与 tokenizer 缓存 | `~/.cache/huggingface/` | 检测模型所需的文本编码资源 |

参考版本：PyTorch 2.3.1、torchvision 0.18.1、transformers 4.57.6、NumPy 1.26.4、rf-groundingdino 0.2.0、rai-gsam2 1.1.3；完整参考与资产指纹见 [runtime-reference.json](../environment/runtime-reference.json)。它是本机版本记录，不是跨机器完整锁文件。

启动脚本依次加载 **系统 Jazzy → 外层 RAI overlay → 本项目 overlay**。overlay 就是工作区编译后的环境层，让 ROS 找到对应包和接口。模型进程再激活 Conda，界面仍使用系统 Python。**日常无需自己 source 三遍或激活 Conda**，也不要先叠加 Kilted 等其他发行版环境。

### 启动配置在哪里、怎么改

配置文件是 `~/.config/produce_order_workstation/launch.env`，采用可信 Bash 语法，仅使用自己编写的配置。**这台电脑已经准备好，不要用模板覆盖，否则可能切换到新数据库。** 新电脑才执行：

```bash
mkdir -p "$HOME/.config/produce_order_workstation"
cp config/launch.env.example "$HOME/.config/produce_order_workstation/launch.env"
chmod 600 "$HOME/.config/produce_order_workstation/launch.env"
```

用编辑器修改此文件；主要配置如下：

| 配置项 | 本机值 / 默认与用途 |
|---|---|
| `PRODUCE_RAI_ROOT` | 本机 `$HOME/rai_embodied_project`；外层 RAI 根目录，不是本项目根目录 |
| `PRODUCE_ROS_SETUP` | 默认 `/opt/ros/jazzy/setup.bash` |
| `PRODUCE_CONDA_SH` | 默认 `$HOME/miniconda3/etc/profile.d/conda.sh`；Conda 初始化脚本 |
| `PRODUCE_CONDA_ENV` | 默认 `rai-jazzy` |
| `PRODUCE_BT_PREFIX` | 上表的 BehaviorTree.CPP 前缀；若已装进系统 ROS，可设为空 |
| `PRODUCE_SIMULATOR_BINARY` | 可省略，默认 RAI 根目录下的 `demo_assets/manipulation/RAIManipulationDemo/RAIManipulationDemo.GameLauncher` |
| `PRODUCE_STATE_DB` | 本机明确使用 `~/.local/share/produce_order_workstation/reservation-fix-validation-20261020.sqlite3`；未设置时默认 `workcell-mcp.sqlite3`，**换路径就是换业务历史** |
| `PRODUCE_ROS_DOMAIN_ID` | 本机 `0`；界面、服务及外部 ROS 客户端必须一致 |
| `PRODUCE_READY_TIMEOUT_S` | 默认 `240` 秒；真实就绪等待上限，不是订单执行超时 |
| `PRODUCE_AGENT_MODEL` | 可覆盖项目配置中的模型 Code；必须账户已开通且支持工具调用，不能凭名称推断免费 |
| `PRODUCE_AGENT_BASE_URL` | 可覆盖百炼兼容接口；与账户地区匹配，国内接口示例 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `PRODUCE_AGENT_PROXY_URL` | 本机 `http://127.0.0.1:7897`；远程模型代理，不需要代理时设 `""`；ROS/MCP 本机通信不走它 |

例如更换账户可用模型，在外部配置中写：

```bash
PRODUCE_AGENT_MODEL="替换为账户实际开通的模型Code"
# 无需代理时才使用下面这行：
PRODUCE_AGENT_PROXY_URL=""
```

**配置生效规则：**终端显式设置的同名 `PRODUCE_*` 变量优先于配置文件；可用 `PRODUCE_LAUNCH_CONFIG` 指向另一份可信配置。持久修改优先编辑文件，不依赖临时 export。服务运行中改配置不会热更新；先按第 7 节安全处理现场，再运行 `restart`。若只是 `start` 且关键配置与运行实例不同，启动器会提示，不会偷偷切库。

### API Key 与数据必须独立保存

Key 单独放在 `~/.config/rai/bailian.env`，不要写进上面的启动配置或 Git。先创建目录，再用编辑器保存以下格式（占位符替换成自己的密钥）：

```dotenv
DASHSCOPE_API_KEY=YOUR_PRIVATE_KEY
```

```bash
mkdir -p "$HOME/.config/rai"
# 用编辑器创建/编辑 ~/.config/rai/bailian.env 后：
chmod 600 "$HOME/.config/rai/bailian.env"
```

Agent 从私有文件读取 Key；启动器不把 Key 转发进 systemd 可查看的 Environment 字段。不要把真实 Key 放进命令历史、截图或日志。`doctor` 只检查凭据文件存在性和权限，**不证明额度、区域或模型可调用**。

SQLite 与报告保留在 `~/.local/share/produce_order_workstation/`，不属于源码。重启不删库，清空日志不删订单；不要通过删除数据库来绕过恢复确认。

### 新用户配置与下载顺序

以下只用于**还没有环境的新用户**；本机使用者直接进入第 1 节。按现有可运行环境提供步骤，不额外进行新环境安装验证。驱动/CUDA 需与自己的 NVIDIA 显卡匹配；本文不保证不同硬件完全一致。

A～G 尽量在同一终端依次执行；如果中途换终端，先恢复 `RAI_ROOT`、`WORKSTATION` 和外部 BT 前缀变量，再加载 Jazzy。所有路径以自己的安装位置为准。

**A. 准备系统。** 安装 Ubuntu 24.04 图形桌面、NVIDIA 驱动、Miniconda，以及 [ROS 2 Jazzy](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html)。配置好 ROS apt 源后：

```bash
sudo apt-get update
sudo apt-get install build-essential cmake git git-lfs wget unzip python3-colcon-common-extensions python3-vcstool python3-rosdep python3-pyqt5 ros-jazzy-moveit ros-jazzy-moveit-resources-panda-moveit-config ros-jazzy-depth-image-proc
git lfs install
source /opt/ros/jazzy/setup.bash
```

**B. 获取本 Fork 的 `development` 分支，以及外层 RAI。** 不要下载 RobotecAI 上游默认分支代替本分支。

```bash
export RAI_ROOT="$HOME/rai_embodied_project"
export WORKSTATION="$RAI_ROOT/src/examples/rai-manipulation-demo"
git clone https://github.com/RobotecAI/rai.git "$RAI_ROOT"
git -C "$RAI_ROOT" checkout 6802d4073e8caa2ab72c5509fa1eeeb659663f64
mkdir -p "$RAI_ROOT/src/examples"
git clone --branch development https://github.com/woc789/rai-manipulation-demo.git "$WORKSTATION"
git -C "$WORKSTATION" lfs pull
git -C "$RAI_ROOT" apply --check "$WORKSTATION/patches/rai-workcell.patch"
git -C "$RAI_ROOT" apply "$WORKSTATION/patches/rai-workcell.patch"
```

仅在新目录执行，不覆盖现有环境。RAI 补丁是本分支依赖的一部分，不能省略；不需要另 Fork RAI 才能使用本分支。

**C. 补齐 ROS 和行为树依赖。**

```bash
cd "$RAI_ROOT"
vcs import src < ros_deps.repos
# 只有从未初始化 rosdep 的机器才执行 sudo rosdep init
rosdep update
rosdep install --from-paths "$RAI_ROOT/src/src/rai_interfaces" "$RAI_ROOT/src/rai_bringup" "$WORKSTATION/ros2_ws/src" --ignore-src -r -y
```

rosdep 输出有失败项必须处理。BehaviorTree.CPP 本机使用 **4.10.0**，不是 3.x。本机为避免混装发行版，将 Jazzy deb 解包到独立前缀；新机可按同样结构准备：

```bash
sudo apt-get install libsqlite3-dev libzmq3-dev ros-jazzy-tinyxml2-vendor
mkdir -p "$HOME/.local/share/rai-deps/behaviortree-jazzy"
cd "$HOME/.local/share/rai-deps/behaviortree-jazzy"
apt-cache policy ros-jazzy-behaviortree-cpp
apt-get download ros-jazzy-behaviortree-cpp
# 将下面占位符换成上一条实际下载的 deb 文件名：
dpkg-deb -x ./DOWNLOADED_BEHAVIORTREE_JAZZY.deb ./prefix
export PRODUCE_BT_PREFIX="$PWD/prefix/opt/ros/jazzy"
export CMAKE_PREFIX_PATH="$PRODUCE_BT_PREFIX:${CMAKE_PREFIX_PATH:-}"
export LD_LIBRARY_PATH="$PRODUCE_BT_PREFIX/lib:${LD_LIBRARY_PATH:-}"
```

apt 源若不再提供本机相同版本，记录实际版本；不要拿其他 ROS 发行版的包替代。上面的外部前缀随后要写进启动配置。

**D. 创建 Conda 并安装 Python 依赖，然后编译。**

```bash
source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda create -n rai-jazzy python=3.12 pip -y
conda activate rai-jazzy
python -m pip install numpy==1.26.4 torch==2.3.1 torchvision==0.18.1 transformers==4.57.6 scikit-learn==1.3.2 rf-groundingdino==0.2.0 rai-gsam2==1.1.3
python -m pip install -e "$RAI_ROOT/src/rai_core" -e "$RAI_ROOT/src/rai_sim" -e "$RAI_ROOT/src/rai_extensions/rai_perception" -r "$WORKSTATION/requirements/runtime-extra.txt"
conda deactivate
source /opt/ros/jazzy/setup.bash
cd "$RAI_ROOT"
colcon build --symlink-install --packages-select rai_interfaces rai_bringup
source "$RAI_ROOT/install/local_setup.bash"
cd "$WORKSTATION/ros2_ws"
colcon build --symlink-install
source "$WORKSTATION/ros2_ws/install/local_setup.bash"
```

Python 包依赖会联网下载，PyTorch 分发包与驱动需匹配；ROS 构建使用系统 Python，不使用另一 Python 版本替代。编译成功后才继续。

**E. 下载运行资源。** 这些不在源码仓库中；API 模型不需要本地下载。

| 资源 | 下载方式 | 大小与保存位置 |
|---|---|---|
| O3DE Manipulation Jazzy 完整包 | 下方 `download_demo.sh manipulation`，来自 RobotecAI 发布地址 | 大型压缩包；大小以下载显示为准，保留解压完整目录 `RAI_ROOT/demo_assets/manipulation/` |
| GroundingDINO 权重 | `download-perception-models` | 693,997,677 字节，`~/.cache/rai/vision/weights/` |
| SAM2 large 权重 | 同一命令 | 897,952,466 字节，同上 |
| BERT tokenizer 与模型 | 下方 Hugging Face 命令 | 模型约 0.44 GB，加 tokenizer；`~/.cache/huggingface/` |

```bash
cd "$RAI_ROOT"
source /opt/ros/jazzy/setup.bash
bash scripts/download_demo.sh manipulation
source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda activate rai-jazzy
source "$RAI_ROOT/install/local_setup.bash"
download-perception-models
python -c 'from transformers import AutoTokenizer; AutoTokenizer.from_pretrained("bert-base-uncased")'
python -c 'from huggingface_hub import hf_hub_download; hf_hub_download("bert-base-uncased", "model.safetensors")'
```

权重约 2 GB，另计仿真包、源码/LFS、Conda 和 CUDA/PyTorch 下载；**不是只下载 Git 仓库就有完整运行资源**。下载需要可访问对应站点；正式启动离线读取视觉缓存。上游仿真下载脚本会替换已有 manipulation 目录，因此只在首次准备时执行，不要当作日常启动命令。

**F. 写配置。** 按本节上面的模板操作，把 `PRODUCE_RAI_ROOT`、Conda、BT 前缀和新数据库路径填好；新用户不使用本机历史验证库。独立保存自己的 Key，选择已开通的工具调用模型，代理仅按实际需要填写。不要照抄本机 `7897` 代理。

**G. 启动并看效果。** 在已登录桌面的终端：

```bash
cd "$WORKSTATION"
bash start.sh doctor
bash start.sh start
```

doctor 通过后启动；等待 READY，看到仿真、订单窗口和桌面物品，再返回 [README](../README.md) 按启动与下单步骤填写混合订单。本流程按现有环境整理，未额外执行新环境安装验证。
