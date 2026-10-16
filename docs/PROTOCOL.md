# 协议与命令

命令在仓库目录执行。全局 `--state` 指定私有状态目录，放在子命令之前；skill 启动器使用安装时记录的目录。

## 身份与路由

首次通过 bridge_pair 进行设备配对，DSH ID 取自工具调用的实际上下文。每个 Codex 聊天另有 connection；connect/create 仅改变调用方的对应关系。

submit 默认读取 CODEX_THREAD_ID 和当前 connection。任务持久保存 task_id、原 Codex ID、目标 DSH ID、指令、版本与展示名称；重试必须保留同一 ID、内容和路由。换连接不会改写已提交的任务。只有用户明确指定另一个回传聊天时才用 `--reply-thread ID --session DSH_ID`。

## 创建与查询

```sh
python3 bridge.py sessions --search '应用回归测试'
python3 bridge.py read --session '实际ID' --messages 6
python3 bridge.py connect --session '实际ID'
python3 bridge.py create --workspace 'E:\project'
python3 bridge.py create --workspace 'E:\project' --full-access
python3 bridge.py query-status '查询ID'
```

查询独立于模型任务队列，不启动目标模型。列表最多 50 项，先按标题/目录匹配，无结果时用 DSH 搜索补充。read 仅返回有界的用户/助手消息，含截断和分页标记；更早历史使用 `--before-seq N`。

创建请求先保存固定 session ID 和权限选择，再由 Windows 验证目录并创建。默认 permission_preset=inherit，不改 DSH 权限；显式 --full-access 才选择 danger-full-access，核验 sandbox=danger-full-access、approval=never 后持久化。Mac 收到身份、权限选择匹配的回执才保存 connection。

创建要求协议 4，防止旧插件把默认创建当作 Full access。默认等待 20 秒，超时返回 waiting_for_windows 和查询 ID；应 query-status 等结果，不重复创建。失败返回 result.ok=false；若已创建但后续权限失败，先检查固定 ID，不自动额外建会话。

## 任务与附件

```json
{
  "name": "应用回归测试",
  "stage": "Windows 复测",
  "instructions": "已授权的操作、边界、验收标准与需要提供的实际证据。",
  "workspace": "E:\\project",
  "revisions": {}
}
```

instructions 最多 50000 字符；name 最多 120、stage 最多 40，均为单行文本。代码任务的 revisions 填真实完整小写 SHA（40 或 64 位），环境检查可留空。旧任务无名称和阶段时按编号和“执行”显示。

```sh
python3 bridge.py submit --file /实际私有路径/task.json
python3 bridge.py status --task '任务ID'
```

插件每 3 秒领任务，以 task_id 作为 DSH requestId，以 queue 模式送入目标桌面会话。任务在单台 Windows 串行执行，接受前跨过 DSH 持久化屏障。恢复时使用原 requestId，不为有副作用的执行设置到期后自动重跑的租约。

DSH 调用 bridge_report：

| 字段 | 含义 |
| --- | --- |
| task_id | 当前任务 ID |
| outcome | passed / failed / blocked |
| summary | 实际命令、输出、版本和结论；入队不等于执行成功 |
| commits | `[{"repo":"项目名","sha":"实际完整SHA"}]`；passed 必须匹配任务要求 |
| files | Windows 任务工作目录内文件的绝对路径数组，无附件时为 `[]` |

附件最多 10 个，每个 8 MiB、总计 20 MiB；符号链接最终目标也须在任务目录内。Mac 保存真实附件字节，Markdown 按普通文件处理。Mac → Windows 没有独立附件端点，短文本可纳入指令并标注为资料。

Windows 先持久保存 outbox 再上传。Mac 按 task_id 和结果内容去重；相同结果重复上传不重复通知，内容冲突会拒绝。

## 通知与 ACK

通知显示编号、名称、阶段和状态。Mac 调用原桌面所用的 codex queue；CLI 回执表示已入队，不等于原聊天已处理。

```sh
python3 bridge.py receive '任务ID'
python3 bridge.py ack '任务ID'
```

receive 校验当前 CODEX_THREAD_ID。已 ACK 返回 action=skip，不提供报告正文、路径或后续命令；结束该通知处理。未 ACK 返回 action=process、result_file 和 ack_command；核验后才 ACK。ACK 不是业务验收通过的声明。

ACK 不撤回已入 Codex 队列的消息；每次收件仍需检查。重复结果不清除 ACK，晚到的投递回执不能覆盖 ACK。

| 状态 | 含义与动作 |
| --- | --- |
| task pending / accepted | 待执行或已接受；长时间无结果时检查 DSH 权限、工具或用户输入 |
| notification pending / delivering | 待投递或正在投递，不另发同一结果 |
| queued | CLI 确认入队，等待原聊天核验与 ACK |
| uncertain | 失败、超时或投递中断；先查原聊天与回执，不自动重发 |
| acknowledged | 原聊天已确认；延迟通知跳过 |

确认确需重发后才 retry-notification TASK_ID。Queue 每次调用产生新消息 ID，不能保证恰好投递一次；执行到一半的外部副作用也不能仅靠消息去重保证恰好一次。

原聊天运行时 Queue 等本轮结束。当前工作依赖结果时主动查询并 receive/ack；没有独立工作时结束本轮，等自动续作。Steer 未启用：已测试桌面运行时没有默认控制 socket，另起 app-server 无法控制原会话，历史状态不能代替实时状态。

## 维护与升级

pause 停止派发新任务但允许结果上传；`pause --allow-task ID` 仅放行指定维护任务；resume 恢复。不要因升级而取消用户工作。

`bundle --update` 生成私人更新包。默认升级是保存工作、完全退出 DSH、运行新 setup.ps1、正常重启。高级 update-when-idle.ps1 要求 Mac 已暂停且 Windows active/outbox 清空，只替换独立插件与 patch，不强杀应用；切换至 worker-v4.mjs 后仍需正常重启。必须验证 worker.protocol=4 和查询结果，不以复制成功代替加载成功。
