# 安装、连通验证与卸载

目标是让原 Mac Codex 聊天发任务、Windows DSH 执行、结果回到原聊天并完成 ACK。只完成文件复制或消息入队，不算验收通过。

以下流程也供 README 的启动 prompt 使用。Codex 应先检查，再做已授权步骤；需要本人登录、切换 Windows 或保存并退出 DSH 时给出明确操作，等待完成后继续。保留用户已有桥、任务、配置和个人 skill，不直接覆盖它们。

## 1. 检查前提和已有安装

Mac 需要 Python 3.9+、Tailscale 和可用的 Codex 桌面聊天。Windows 需要 Tailscale、已启动过一次的 DSH 桌面应用和已配置的模型。DSH 必须支持本项目依赖的桌面插件接口；此桥不负责安装模型或写入模型 API 密钥。

在 Mac Codex 中打开下载好的仓库。先检查：

```sh
python3 --version
python3 bridge.py --help
```

找出**当前桌面使用的** Codex CLI，验证版本和 queue 命令。不能只凭 PATH 中另一个 codex 来判断桌面能力。一种已验证的安装位置如下，实际位置不同时用真实路径：

```sh
CODEX_CLI='/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
"$CODEX_CLI" --version
"$CODEX_CLI" queue --help
```

若没有 `queue --thread --message`，先解决版本兼容，不能另起 app-server 冒充原桌面会话。自动 Steer 不属于当前安装流程。

检查以下位置是否已有安装：

- Mac：所选状态目录（默认 `~/.local/share/codex-dsh-bridge`）、`~/Library/LaunchAgents/local.codex-dsh-bridge.plist`。
- skill：`${CODEX_HOME:-$HOME/.codex}/skills/windows-dsh`。
- Windows：`%USERPROFILE%\.codex-dsh-bridge`，以及 DSH 的 Desktop profile patch。

已有安装时先读取不含凭据的 status、核对版本与运行路径；不要 init、生成新令牌或覆盖安装。若确需升级，先明确迁移范围，再按 [维护流程](PROTOCOL.md#维护与升级) 操作。使用新目录也不能在同一端口启动第二个桥。

## 2. 建立 Tailscale 网络

两台机器按 [Tailscale 官方快速入门](https://tailscale.com/docs/how-to/quickstart) 安装、由本人完成登录，加入同一个可互通的 tailnet。网络策略需要允许 Windows 访问 Mac 的 TCP 18766（或实际配置端口），不需要路由器端口映射或公网 IP。

按 [官方 CLI 说明](https://tailscale.com/docs/reference/tailscale-cli) 找到可执行文件；若 PATH 没有 tailscale，使用实际安装路径。分别执行：

```sh
tailscale status
tailscale ip -4
tailscale ping <对方的TailscaleIPv4>
```

记录双方实际 IPv4。ping 证明 Tailscale 路径可达；后面 Windows 的 HTTP /health 才能验证桥端口。如果登录需要验证码或浏览器授权，由本人完成，不把密码或认证码写入配置和任务。

## 3. 初始化 Mac

只对全新安装执行。本节与后面的 Mac shell 命令在同一终端执行；若通过 Codex 分次运行，使用实际值重新设置变量，不能假定跨 shell 保留变量。

```sh
BRIDGE_STATE="$HOME/.local/share/codex-dsh-bridge"
MAC_TS_IP='<Mac实际TailscaleIPv4>'
WINDOWS_TS_IP='<Windows实际TailscaleIPv4>'
python3 bridge.py --state "$BRIDGE_STATE" init \
  --host "$MAC_TS_IP" --worker-ip "$WINDOWS_TS_IP" \
  --codex-cli "$CODEX_CLI"
```

在 Codex 执行环境内，init 从 CODEX_THREAD_ID 取得当前聊天。普通终端没有该变量时，需要由原 Codex 聊天确认真实 ID，再显式传 `--thread`；不要复制教程里的示例 ID。配置不覆盖已有文件，令牌在本机随机生成，无需用户手工填写。

## 4. 启动 Mac 后台服务

服务与 Codex 须属于同一 macOS 用户，并使用相同 CODEX_HOME。仓库须保留在稳定目录，移动后服务和 skill 路径需要重新核对。下面的生成步骤拒绝覆盖已有 LaunchAgent；它只写启动配置，不会启动服务：

```sh
python3 - "$BRIDGE_STATE" <<'PY'
import os, pathlib, plistlib, sys
repo = pathlib.Path.cwd().resolve()
state = pathlib.Path(sys.argv[1]).expanduser().resolve()
if not (repo / 'bridge.py').is_file() or not (state / 'config.json').is_file():
    raise SystemExit('请在仓库目录执行，并先完成 init')
destination = pathlib.Path.home() / 'Library/LaunchAgents/local.codex-dsh-bridge.plist'
destination.parent.mkdir(parents=True, exist_ok=True)
data = {
    'Label': 'local.codex-dsh-bridge',
    'ProgramArguments': [sys.executable, str(repo / 'bridge.py'), '--state', str(state), 'serve'],
    'WorkingDirectory': str(repo),
    'EnvironmentVariables': {'CODEX_HOME': os.environ.get('CODEX_HOME', str(pathlib.Path.home() / '.codex'))},
    'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
    'StandardOutPath': str(state / 'service.log'),
    'StandardErrorPath': str(state / 'service-error.log'),
}
with destination.open('xb') as file:
    plistlib.dump(data, file)
print(destination)
PY
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.codex-dsh-bridge.plist"
launchctl print "gui/$(id -u)/local.codex-dsh-bridge"
```

生成或 bootstrap 失败时先读错误，不继续假装安装成功。已有同名服务时不要直接 bootout。Tailscale 未在线时绑定地址会失败，服务可稍后重试；检查状态目录的 service-error.log，不在公开 Issue 粘贴完整私有日志。

Windows PowerShell 验证桥端口（替换实际 IP）：

```powershell
curl.exe --noproxy "*" --connect-timeout 10 --max-time 15 http://<Mac实际TailscaleIPv4>:18766/health
```

只有配置的 Windows 来源 IP 能访问 /health。必须收到 JSON 的 `ok: true`；超时则核对地址、进程、Tailscale 策略、macOS 防火墙和代理，不关闭整机防火墙来绕过。

## 5. 生成并安装 Windows 私人包

Mac：

```sh
python3 bridge.py --state "$BRIDGE_STATE" bundle
```

保留返回的临时 URL 和 SHA-256，发送给自己控制的 Windows。URL 一小时有效且限制来源 IP，配对后关闭下载；包内含长期有效令牌，不能公开或上传 Release。

先在 Windows 保存工作并完全退出 DSH，再在 PowerShell 执行下列命令，将两处值替换为 Mac 本次输出：

```powershell
$ErrorActionPreference = 'Stop'
$bundleUrl = '<Mac本次生成的URL>'
$expectedSha = '<Mac本次输出的SHA256>'
$bundleDir = Join-Path $env:TEMP ('codex-dsh-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $bundleDir | Out-Null
$archive = Join-Path $bundleDir 'windows.zip'
curl.exe --fail --show-error --noproxy "*" --connect-timeout 10 --max-time 60 --output $archive $bundleUrl
if ($LASTEXITCODE -ne 0) { throw '下载失败，停止安装' }
if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedSha.ToLowerInvariant()) { throw 'SHA256 不一致，停止安装' }
$unpacked = Join-Path $bundleDir 'unpacked'
Expand-Archive -LiteralPath $archive -DestinationPath $unpacked
& (Join-Path $unpacked 'setup.ps1')
```

若本次脚本被执行策略阻止，可对已经核对的脚本单独运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <setup.ps1实际路径>`；这不修改系统永久执行策略。自定义 DSH Home 用 `-DshHome 'D:\实际目录'`。

安装器只追加用户 Desktop 的 cordis.patch.yml 并保留备份，不修改 DSH 安装文件或模型设置。复杂 YAML 布局会拒绝自动改写，应让 Codex检查并合并独立插件条目，不能覆盖整个文件。配置和插件位于 `%USERPROFILE%\.codex-dsh-bridge`。

## 6. 配对和选择会话

重新双击打开 DSH，选择一个用于首次配对的普通会话（也可以是用户创建的空会话），输入：

> 请调用 bridge_pair，把当前会话与已配置的 Mac Codex 会话配对；不要创建其他会话，只完成这次配对。

工具从实际上下文读取会话 ID。若 DSH 通过 run_code 调工具，遵循其当前工具模式。配对不启动业务任务，也不提升权限。

Mac 检查：

```sh
python3 bridge.py --state "$BRIDGE_STATE" status
python3 bridge.py --state "$BRIDGE_STATE" sessions
```

须看到已配对、Windows worker.protocol=5、seen_at 更新及可用的会话列表。状态无更新时先检查插件加载和配置，不能直接发送任务。

然后选择目标：

- **已有会话**：按用户标题查询、核对目录和历史，用 connect 保存当前 Codex 的对应关系；多个候选先由用户消歧。
- **新会话**：询问 Windows 上已有的工作目录，默认沿用 DSH 权限；只在用户明确选择后加 --full-access。

```sh
python3 bridge.py --state "$BRIDGE_STATE" sessions --search '用户给出的标题'
python3 bridge.py --state "$BRIDGE_STATE" read --session '核对后的ID' --messages 6
python3 bridge.py --state "$BRIDGE_STATE" connect --session '核对后的ID'
# 或按用户选择创建，不要两者都执行：
python3 bridge.py --state "$BRIDGE_STATE" create --workspace 'E:\project'
# 只有明确选择 Full access 时才替换为：
python3 bridge.py --state "$BRIDGE_STATE" create --workspace 'E:\project' --full-access
```

创建会按规范路径复用或登记工作区；回执还须确认 `workspace_id`、`workspace_path`、`workspace_title`、`workspace_attached=true`，且 `cwd` 等于工作区路径。已有 Ungrouped 会话不迁移。

create 超时只查 query-status，不重复建会话。默认回执应为 permission_preset=inherit；Full access 回执须同时确认 danger-full-access 与 approval=never。继承可能遇到工具审批，也可能继承 DSH 已有 Full access，不能隐瞒实际权限。

## 7. 安装通用 skill

```sh
python3 scripts/install-skill.py --state "$BRIDGE_STATE"
```

它将通用 skill 安装到 CODEX_HOME/skills/windows-dsh（未设置 CODEX_HOME 时为 ~/.codex/skills/windows-dsh），并生成仅本机使用的 installation.json。该记录只含仓库和状态路径，不含认证令牌。

同样内容重复安装无害；已有不同的个人 skill、空目录或符号链接会被拒绝，不覆盖。此时先核对已有 skill，可继续使用已有安装或经用户选择后备份迁移；不要为完成流程强制删除。也可用 `--destination` 安装到用户指定位置，是否自动发现取决于该位置是否位于 Codex skill 目录。

新聊天可以用 $windows-dsh。当前聊天若尚未刷新 skill 列表，Codex 可直接读取安装器输出目录中的 SKILL.md 并按它执行。移动仓库后不要继续使用失效安装记录。

## 8. 验收完整闭环

从需要接收结果的原 Codex 聊天执行：

```sh
python3 bridge.py --state "$BRIDGE_STATE" submit --file examples/smoke.json
```

记录返回的 task_id。示例仅要求 Windows 读取操作系统和时间，不访问业务仓库。等待 DSH 调用 bridge_report 后，检查任务状态：

```sh
python3 bridge.py --state "$BRIDGE_STATE" status --task '实际task_id'
```

权限阻塞就报告 blocked 或请用户处理，不能为“通过验收”自动启用 Full access。原 Codex 若一直运行，Queue 会继续排队；无独立工作时结束本轮，让通知启动后续轮次。

收到通知后在原聊天先 receive，核对 Windows 实际输出和标记，再执行返回的 ack_command：

```sh
python3 bridge.py --state "$BRIDGE_STATE" receive '实际task_id'
python3 bridge.py --state "$BRIDGE_STATE" ack '实际task_id'
python3 bridge.py --state "$BRIDGE_STATE" receive '实际task_id'
```

验收证据必须包含：Windows 实际执行、结果已保存、通知出现在发起的同一个 Codex 聊天、ACK 成功、最后 receive 返回 action=skip。仅主动读取并 ACK 可证明结果处理，但不能代替通知自动续作的验证。

若本次新建了会话，还要在首条测试消息后核对它位于指定工作区分类下，并在刷新后再次确认。空会话可能暂不显示，不重复创建。桌面版没有刷新入口时，等其他任务结束后正常退出并重开 DSH；无需调整网页认证。

## 停止和卸载

仅在用户要停止这套安装且已核对对应关系时执行：

```sh
launchctl bootout "gui/$(id -u)/local.codex-dsh-bridge"
```

需要不再随登录启动时，备份并移走对应 plist。Windows 先退出 DSH，再用原包的 `setup.ps1 -Uninstall` 禁用插件；配置和结果保留，避免丢失未回传任务。skill 删除前确认是此次安装，不删除用户原有版本。Tailscale 和双方模型权限无需修改。
