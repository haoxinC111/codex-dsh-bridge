# Codex ↔ DSH Bridge

**在 Mac 上开发，让 Windows 上的 DSH 执行任务，结果自动回到发起任务的 Codex 聊天。**

适合这样的场景：你在 Mac Codex 中改代码，但测试依赖 Windows 软件、桌面应用或运行环境。不用再手动复制指令、寻找会话 ID、粘贴测试报告。

两台设备通过 **Tailscale 私网**连接，无需公网 IP。代码仍可通过 Git 同步；这座桥负责传递任务、连接会话、回传结果。

[Prompt 安装](#prompt-install) · [手动安装](#manual-install) · [日常使用](#daily-use) · [兼容性](#compatibility)

<a href="docs/assets/bridge-flow.svg"><img src="docs/assets/bridge-flow.svg" width="960" alt="任务从 Mac Codex 发起，经通信桥交给 Windows DSH 执行，报告与附件回到原 Codex 聊天。"></a>

## 你可以用它做什么

| 你的需求 | 通信桥如何处理 |
| --- | --- |
| “去 Windows 跑一次测试” | 在指定工作目录新建 DSH 会话，发送任务并取回实际结果 |
| “继续上次那个会话” | 按标题或目录查找会话，核对历史后连接，无需事先知道 ID |
| “那个会话已经做完了，拿回结论” | 读取已有会话的近期记录；需要文件时，让 DSH 回传报告或附件 |
| “我在另一个 Codex 聊天里也要用” | 每个聊天独立绑定目标 DSH 会话，结果回到各自的发起聊天 |
| “把测试报告带回来” | Windows 可回传 Markdown、日志和截图，保存到 Mac 本机 |

## 一次任务如何完成

1. **选会话。** Codex 知道自己的聊天 ID；通过桥查询已有 DSH 会话，或在你指定的 Windows 目录下创建一个。
2. **发任务。** 桥保存任务内容、发起聊天 ID 和目标 DSH 会话 ID。Windows 插件领取后交给该会话执行。
3. **收结果。** DSH 回传状态、实际输出和附件，Mac 保存结果，并通知原 Codex 聊天。
4. **确认处理。** Codex 核验报告后发送 ACK（收件确认）。同一个任务已 ACK 时，后续通知直接跳过。

**回传时机：** 原 Codex 聊天运行中，通知进入 Queue，等本轮结束后处理；聊天空闲时，通知触发后续轮次。当前没有接入自动 Steer。等待期间仍可主动查询结果。

## 安装

两种方式安装的是同一套工具，选择一种即可。

| 方式 | 适合谁 | 如何开始 |
| --- | --- | --- |
| **Prompt 引导安装** | 希望 Codex 检查环境、执行 Mac 操作，并逐步指导 Windows 操作 | 复制[下面的一句话](#prompt-install)到 Mac Codex |
| **手动安装** | 希望自己执行命令、核对每一步配置 | 从[手动流程](#manual-install)开始 |

需要先准备：Mac 上可用的 Codex 桌面聊天和 Python 3.9+；Windows 上已启动过、已配置模型的 **DeepSeek Harness（DSH）桌面应用**；两端均安装 Tailscale。具体接口要求见[兼容性](#compatibility)。本项目不包含这些应用或模型。

<a id="prompt-install"></a>
### 方式一：复制 prompt，让 Codex 带你安装

在 **Mac Codex** 中粘贴这句话：

> 请按 https://github.com/haoxinC111/codex-dsh-bridge 的 README 和 docs/INSTALL.md，帮我检查环境并安装 Mac Codex ↔ Windows DSH 通信桥，完成 Tailscale 连通、两端安装、通用 skill 配置、目标会话选择和只读回传验收，确认结果回到当前聊天且完成 ACK；保留已有配置和个人 skill，默认继承权限，Full access 需我显式选择，遇到账号登录或 Windows 操作时逐步指导我。

Codex 会按这个顺序带你走完：

**检查环境 → 连接两台设备 → 安装两端 → 选择会话 → 只读验收**

你仍需完成账号登录，以及 Windows 上的安装、退出或重启 DSH。Prompt 是引导安装入口，不能跳过这些人工步骤，也不能保证所有应用版本都兼容。如果已经下载仓库，也可以在 Codex 中打开该目录后使用同一段 prompt。

<a id="manual-install"></a>
### 方式二：手动安装

以下是**全新安装**的最短验证流程，先以前台服务跑通，再设置开机后台运行。已有安装请先看[检查与保留已有配置](docs/INSTALL.md#1-检查前提和已有安装)，不要重复初始化。

#### 1. 下载仓库，检查桌面 CLI

在 Mac 终端执行，将仓库放在之后不会移动的目录：

```sh
git clone https://github.com/haoxinC111/codex-dsh-bridge.git
cd codex-dsh-bridge
python3 --version

# 这是已验证过的位置；以你当前桌面应用实际使用的 CLI 为准。
CODEX_CLI='/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
"$CODEX_CLI" queue --help
```

须确认 CLI 支持 `queue --thread --message`。PATH 中其他版本的 `codex` 不能代替桌面 CLI；不支持时先处理兼容问题。

#### 2. 连通 Tailscale，确定回传聊天

两端登录同一个可互通的 tailnet，分别运行：

```sh
tailscale ip -4
tailscale ping <对方的TailscaleIPv4>
```

记下双方 IPv4。网络策略须允许 Windows 访问 Mac 的 TCP **18766**。安装及路径问题见 [Tailscale 设置](docs/INSTALL.md#2-建立-tailscale-网络)。

在**希望接收结果的 Mac Codex 聊天**中说：“请读取当前执行环境的 CODEX_THREAD_ID，告诉我真实的聊天 ID。”将返回值用于下一步；不要使用示例 ID。

#### 3. 启动 Mac 桥

继续在刚才的 Mac 终端执行，替换三个占位值：

```sh
export CODEX_THREAD_ID='<刚才确认的Codex聊天ID>'
python3 bridge.py init \
  --host '<Mac的TailscaleIPv4>' \
  --worker-ip '<Windows的TailscaleIPv4>' \
  --codex-cli "$CODEX_CLI"
python3 bridge.py serve
```

保留这个终端运行。默认状态目录为 `~/.local/share/codex-dsh-bridge`，不要放进 Git 仓库。自定义目录使用全局参数 `--state /实际目录`，后续每次调用均须保持一致。

在 Windows PowerShell 检查服务：

```powershell
curl.exe --noproxy "*" --connect-timeout 10 --max-time 15 http://<Mac的TailscaleIPv4>:18766/health
```

看到 `"ok": true` 再继续。

#### 4. 安装 Windows 插件

另开一个 Mac 终端，进入同一个仓库，生成只属于这两台设备的安装包：

```sh
python3 bridge.py bundle
```

命令会返回下载 URL 和 SHA-256。**包内含设备令牌，只在自己的设备间使用，不要上传 GitHub 或分享链接。**

Windows 先保存工作并完全退出 DSH，再按[安装 Windows 私人包](docs/INSTALL.md#5-生成并安装-windows-私人包)中的 PowerShell 命令，依次完成：

**下载 → 核对 SHA-256 → 解压 → 运行 `setup.ps1` → 重新打开 DSH**

安装器将插件放入用户目录，并为 DSH 添加插件配置；不会替换 DSH 应用或模型配置。详细指南含可直接复制的完整命令。

#### 5. 配对两端，选择目标会话

在 Windows DSH 的一个普通会话中输入：

```text
请调用 bridge_pair，把当前会话与已配置的 Mac Codex 会话配对；不要创建其他会话，只完成这次配对。
```

在第二个 Mac 终端中设置同一个真实聊天 ID，然后核对配对状态：

```sh
export CODEX_THREAD_ID='<同一个Codex聊天ID>'
python3 bridge.py status
python3 bridge.py sessions
```

应看到已配对、`worker.protocol=4`、近期 `seen_at` 和可用的会话列表。接下来二选一：

**A. 连接已有会话：** 先查标题，再读历史，最后连接核对后的 ID。

```sh
python3 bridge.py sessions --search '应用回归测试'
python3 bridge.py read --session '<核对后的DSH会话ID>' --messages 6
python3 bridge.py connect --session '<核对后的DSH会话ID>'
```

**B. 或新建会话：** 填写 Windows 上实际存在的工作目录。

```sh
python3 bridge.py create --workspace 'E:\project'
```

默认继承 DSH 权限。**只有明确选择 Full access 时**，才在 `create` 后追加 `--full-access`；它只影响此次新会话。创建超时后使用回执中的查询 ID 执行 `query-status`，不要重复创建。

#### 6. 安装 skill，完成一次只读验收

在第二个 Mac 终端执行：

```sh
python3 scripts/install-skill.py
python3 bridge.py submit --file examples/smoke.json
python3 bridge.py status --task '<返回的task_id>'
```

通用 skill 默认安装到 `~/.codex/skills/windows-dsh`；设置了 `CODEX_HOME` 时使用该目录下的 `skills/windows-dsh`。已有不同内容时会停止，避免覆盖。示例任务仅让 Windows 读取操作系统和时间。

验收须确认：Windows 实际执行、结果保存、通知出现在**发起的同一个 Codex 聊天**，该聊天核验后 ACK，再次 `receive` 返回 `action=skip`。操作细节见[完整闭环验收](docs/INSTALL.md#8-验收完整闭环)。若 Codex 仍在运行，先让本轮结束，Queue 才能触发后续轮次。

#### 7. 改为后台运行

验收通过后，在第一个 Mac 终端按 `Ctrl+C` 停止前台桥。继续在仓库目录设置状态路径，再按[配置 LaunchAgent](docs/INSTALL.md#4-启动-mac-后台服务)生成并启动后台服务：

```sh
BRIDGE_STATE="$HOME/.local/share/codex-dsh-bridge"
```

如果前面自定义了状态目录，这里也要使用同一个实际路径。先停止前台进程，避免两个服务争用端口。DSH 也需要保持运行。

完整安装、权限说明和卸载步骤统一维护在 [docs/INSTALL.md](docs/INSTALL.md)。

<a id="daily-use"></a>
## 安装后怎么用

在需要接收结果的 Codex 聊天中调用 **`$windows-dsh`**。换一个聊天也能用；每个聊天单独选择目标会话。

**新建一个测试会话：**

```text
使用 $windows-dsh，在 Windows 的 E:\project 下新建会话，沿用 DSH 默认权限，执行只读环境检查，把实际输出回传到当前聊天。
```

**连接已有会话，无需知道 ID：**

```text
使用 $windows-dsh，查找标题为“应用回归测试”的 Windows 会话，核对目录和最近记录后连接，告诉我它已经完成了什么。
```

**继续派发工作，并取回文件：**

```text
使用 $windows-dsh，让已连接的会话在 E:\project 执行现有回归测试，回传实际命令、通过和失败项，并把该目录下的测试报告 Markdown 作为附件传回来。
```

标题匹配多个会话时，先确认目标再继续。继续当前工作时复用已有连接；既没有连接、也没有指定已有会话时，才在你提供的工作目录新建会话。目录不明确时先询问。涉及代码版本的任务还应指定分支或 commit，并说明是否允许 Windows 拉取代码。

<a id="compatibility"></a>
## 兼容性与当前边界

**实验性社区工具，非 OpenAI 或 DeepSeek 官方产品。** 是否可用以实际应用接口为准。

| 项目 | 当前支持范围 |
| --- | --- |
| Mac | Python 3.9+，无需 pip 依赖；服务与 Codex 使用相同系统用户及 Codex 数据目录 |
| Codex | 桌面 CLI 支持 `queue --thread --message`；已实测版本 `0.162.0-alpha.17.2` |
| Windows DSH | 需要 Desktop profile patch、SessionController、tools、会话持久化和权限预设接口；插件使用 DSH 内置 Node |
| 网络与设备 | Tailscale 私网；当前面向单用户、单台 Windows，任务串行执行 |
| 插件协议 | 本发布版为协议 4；新建会话要求协议 4，查询与连接已有会话要求 2+ |
| 自动续作 | 已验证应用开启、原聊天已加载的情况；关闭应用、休眠或未加载聊天不保证及时处理 |
| 文件传输 | Windows → Mac：最多 10 个附件，单个 8 MiB、总计 20 MiB，文件须在任务工作目录内 |
| Mac → Windows | 支持任务文本；尚无独立文件上传接口，代码可继续通过 Git 同步 |
| 权限 | 默认继承 DSH 权限，可能需要审批，也可能继承已有 Full access；显式开启只作用于新会话 |
| 去重 | 相同结果不重复通知，已 ACK 任务不重复处理；不承诺外部操作“恰好执行一次” |

## 文档与开发

- [安装指南](docs/INSTALL.md)：环境检查、双机连通、后台服务、验收与卸载。
- [命令与协议](docs/PROTOCOL.md)：会话路由、任务字段、附件、通知、ACK 和升级。
- [安全说明](SECURITY.md)：令牌、私有状态与公开发布边界。
- [验证记录](docs/VALIDATION.md)：已验证内容与尚未验证的部分。

主要文件：`bridge.py` 是 Mac 服务与 CLI，`windows/` 是 DSH 插件及安装脚本，`skills/windows-dsh/` 是通用 skill，`examples/` 是任务示例。

本地开发测试需要 Python 3.9+ 和 Node.js 20+：

```sh
python3 -m unittest discover -s tests -v
node --test tests/worker.test.mjs
# 安装了 PowerShell 时，可运行隔离的安装器测试：
pwsh -NoProfile -File tests/setup.Tests.ps1
```

这些测试使用临时目录、模拟会话接口和本机 HTTP，不调用真实模型，也不向真实 Codex 聊天投递。已有 DSH 构建目录时，还可运行 `node tests/dsh-runtime-smoke.mjs /实际路径/deepseek-harness`。真实双机验收仍须按安装指南完成。

## 许可证

[MIT License](LICENSE)。第三方应用和服务遵循各自的许可与使用条件。公开仓库仅存放源码和通用示例；私人安装包、配置、数据库、日志、任务结果与附件均不应提交。
