# 实现结构

Agent 的执行循环、命令运行与最终评分属于不同层次。这个仓库主要公开执行层；课程使用的 benchmark 准备与官方回评另行维护。

| 文件 | 职责 |
|---|---|
| `agent.py` | 有界执行循环，任务提示与工具 profile 选择，建议性评审与最终交付 |
| `model.py` | DeepSeek API 请求、流式响应解析与可取消的请求进程 |
| `tool_registry.py` | 工具 schema、参数验证与分发 |
| `tool.py`、`workspace.py` | 合成材料读取、工作区路径检查、文件快照与 SHA256 编辑 |
| `coding_tools.py` | 工作区命令、bubblewrap 文件/网络边界、读写访问策略 |
| `jobs.py` | 任务所属进程、前后台等待、超时、取消、输出和结束清理 |
| `scheduler.py` | 批量工具调用的调度与返回 |
| `runtime_input.py`、`control.py` | 用户追加输入、控制消息和运行中取消 |
| `context.py` | 工具输出节选、完整结果取回与可选 smart_zip |
| `reviewer.py` | 独立上下文的只读评审，返回 accept/revise/cannot_verify 建议 |
| `events.py` | 运行事件与实际模型请求记录 |

## 工具与任务身份

`profile=materials` 使用本地材料工具；`profile=coding` 使用工作区、命令与文件编辑工具。

`task_mode` 默认跟随 profile。coding 工具还可选择 `task_mode=qa`，以保留 Python 和文件能力，同时使用问答执行及评审提示。工具权限不因 QA 身份而扩展，也不因缺少源码 diff 而要求问答任务改代码。

## 作业与证据

命令 `running` 是启动状态，完成后才会取得退出码、输出与停止原因。只读命令可在题内并发，写命令互斥；不能在项目命令运行时编辑工作区。题内 `/tmp` 共享，题间不共享。

reviewer 有自己的只读工具和预算，其建议不决定执行者必须交付或继续。命令执行成功、reviewer 接受、用户需求满足及外部 benchmark 通过是不同事实。

真实工具返回和模型请求写入事件记录。上下文节选不删除原文，可通过工具取回；这些机制提供可核对的执行证据，不保证模型正确使用证据。
