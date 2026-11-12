# Nano Agent

零第三方依赖、可审计、易扩展的轻量级 Python 工具智能体运行时（Agent Runtime）。

Nano Agent 仅基于 Python 标准库与 `bubblewrap`（`bwrap`）构建，将主执行循环、声明式工具注册、异步进程调度、实时调用栈诊断、独立只读评审与事件追踪拆分为十余个职责清晰的单文件模块，便于直接阅读源码与按需扩展。

**文档导航**：[架构与模块说明](docs/architecture.md) · [发布验证记录](docs/validation.md) · [已知限制与待研究问题](docs/limitations.md) · [课程实验与版本演进（README-course）](README-course.md)

---

## 核心机制与扩展入口

模块边界与设计取舍详见 [docs/architecture.md](docs/architecture.md)，已知边界详见 [docs/limitations.md](docs/limitations.md)。

### 1. 模块化内核（二次开发入口）

- **模型后端（`model.py`）**：封装 API 请求、流式增量解析与可取消请求子进程。默认接入 DeepSeek Chat Completions（`deepseek-flash`）；替换模型服务或扩展多后端协议只需修改该层。
- **工具注册与工作区（`tool_registry.py`、`coding_tools.py`、`workspace.py`）**：统一负责工具 schema 生成、参数类型校验（支持轻微畸形 JSON 参数自动恢复）与批量调用分发（`scheduler.py`）。文件编辑带 `SHA256` 乐观锁、冲突摘要回传与相似/多处匹配行号提示，支持 `/work` 路径归一化与 `create_file(..., overwrite=true)`。
- **主循环与独立评审（`agent.py`、`reviewer.py`）**：解耦工具能力集 `profile`（`materials` / `coding`）与任务提示身份 `task_mode`（`materials` / `coding` / `qa`）。无工具的候选回答可送交独立上下文的只读 Reviewer 检查（支持工作区快照预置与预算末轮强制收尾，建议不具硬性否决权）；执行者内置低步数收敛提醒与终轮合成保护（`--ergonomic-v5`）。
- **上下文节选与可审计轨迹（`context.py`、`events.py`、`runtime_input.py`）**：长工具输出自动节选并支持通过 `read_tool_result` 按字符偏移取回完整原文；支持运行中在安全节点按行追加用户指令（`--interactive`）；全部真实模型请求、流式片段、工具回执与控制事件实时落盘为 JSONL。

### 2. 沙箱隔离、异步调度与实时诊断（`jobs.py`）

- **读写访问边界**：工作区命令在 `bwrap` 文件与网络隔离沙箱中运行。`access=read_only` 通过只读挂载强制生效，支持题内并发（`--max-jobs`）且可选不阻塞文件编辑（`--allow-edit-during-readonly`）；`access=write` 独占工作区，且进入候选评审或最终交付前必须等待在途写命令结束。
- **前台自动转后台**：配置 `--foreground-yield-s` 后，前台命令超过观察窗口**不会终止子进程**，而是原地转为后台任务并返回 `job_id` 与已有输出；配合 `--wait-timeout-s` 有界增量等待与后台任务完成通知，避免单条长耗时命令阻塞整个执行循环。
- **带外实时调用栈与优雅中断**：任务运行期间（`--live-diag`）按 `<pid>` 穿透多层 Python `subprocess` 无损采集活跃叶节点实时调用栈与 `/proc` CPU 活性快照，无需杀进程即可区分“正常计算推进”与“死循环/阻塞”；取消或超时停止时（`--stop-with-sigint`）优先发送 `SIGINT` 捕获 `KeyboardInterrupt` 栈回溯，再按宽限期清理回收。

---

## 快速上手与本地测试

### 环境条件

- **系统与 Python**：已验证环境为 **Linux / Python 3.14.9**（仅依赖 Python 标准库，无需 `pip` 安装第三方包；未验证 macOS / Windows）。
- **隔离沙箱**：工作区命令依赖 `bubblewrap`（`bwrap`），并要求运行环境支持网络命名空间隔离（初始化无法建立该边界时拒绝运行）。

### 运行示例

配置 API 凭据（仓库不含任何凭据，亦兼容原学习环境的本机配置回退读取），在内置合成练习项目 `fixtures/todo_cli` 的临时副本上运行：

```bash
export DEEPSEEK_API_KEY='your-api-key'
NANO_WS=$(mktemp -d) && cp -r fixtures/todo_cli/. "$NANO_WS/"

python3 agent.py '修复新建待办事项时的 ID 冲突，保留已有行为，运行项目测试。' \
  --profile coding --workspace "$NANO_WS" \
  --max-calls 40 --run-timeout-s 300 --command-timeout-s 180 --max-command-timeout-s 300 \
  --foreground-yield-s 25 --wait-timeout-s 30 --allow-edit-during-readonly \
  --live-diag --stop-with-sigint --ergonomic-v5 --max-review-calls 6
```

- **其他任务模式**：传入 `--profile coding --task-mode qa --workspace "$NANO_WS"` 可在保留本地 Python/命令工具的同时使用问答提示与问答评审（不要求生成代码 diff）；传入 `--profile materials` 则在内置 `sandbox/` 文本材料上检索作答。
- **运行输出**：每次运行在 `runs/`（已列入 `.gitignore`）下生成汇总报告 `.json` 与增量事件流 `.events.jsonl`。真实运行记录可能包含输入材料与工作区代码，请按发布范围自行管理。

### 无模型回归测试

在仓库根目录执行完整回归测试（无需配置 API Key，使用确定性受控模型响应与真实 `bwrap` 子进程验证协议契约，当前共 117 项，详见 [docs/validation.md](docs/validation.md)）：

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

---

## 命令行参数速查

省略时限参数时 CLI 不自动附加默认时限；上文示例中的秒数与步数仅为使用演示。

| 类别 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| **任务与工作区** | `--profile {materials,coding}` | `materials` | 工具能力集选择 |
| | `--task-mode {materials,coding,qa}` | 跟随 `profile` | 任务提示与评审模式；`qa` 需配合 `--profile coding` |
| | `--workspace DIR` | 无 | `coding` 模式的目标工作区路径 |
| | `--criterion TEXT` | 内置条件 | 替换默认验收条件，可重复指定 |
| **预算与并发** | `--max-calls N` | `8` | 执行者（Actor）的模型请求上限；评审单独计数 |
| | `--run-timeout-s SEC` | 不设上限 | 本次运行总时限（覆盖模型请求与工具执行） |
| | `--command-timeout-s SEC` | 不设上限 | 单条命令默认时限 |
| | `--max-command-timeout-s SEC` | 不设上限 | 单条命令时限硬上限，超限自动向下钳位 |
| | `--max-jobs N` | `4` | 题内最多同时运行的只读命令数 |
| **异步调度与实时诊断** | `--foreground-yield-s SEC` | 关闭 | 前台同步观察窗口秒数，到期不杀进程、自动转后台 |
| | `--wait-timeout-s SEC` | 关闭 | `wait_job` 默认有界等待秒数 |
| | `--allow-edit-during-readonly` | 关闭 | 允许在只读后台命令运行期间编辑工作区文件 |
| | `--live-diag` | 跟随 `--stop-with-sigint` | 运行中任务按需附带叶节点实时调用栈与 CPU 活性快照（默认关闭） |
| | `--stop-with-sigint` | 关闭 | 取消或超时停止时先发 `SIGINT` 捕获调用栈 |
| **评审、收敛与交互** | `--ergonomic-v5` | 关闭 | 启用评审快照预置、末轮强制收尾、后向宽容 JSON 解析与 Actor 步数收敛保护 |
| | `--no-review` | 关闭 | 关闭交付前建议性评审 |
| | `--max-reviews N` | `2` | 单次任务最多触发几次独立评审 |
| | `--max-review-calls N` | `3` | 每次独立评审最多请求模型几次 |
| | `--tool-output-chars N` | `4000` | 请求上下文中单条工具输出字符上限（至少 `256`） |
| | `--no-stream` | 关闭 | 关闭实时流式输出，改用非流式请求 |
| | `--interactive` | 关闭 | 允许运行中按行追加要求（`/now` 尽快处理，`/bg` 或 `Ctrl+B` 转后台） |

---

## 版本清单与许可

- **源码标记与哈希清单**：源码保留原有的 AI 辅助开发标记；受控运行代码、测试与合成示例文件的哈希快照记录于 [`source_manifest.json`](source_manifest.json)。早期研究演进、阶段差异与实验复现说明见 [README-course.md](README-course.md) 与 [docs/experiment-record.md](docs/experiment-record.md)。
- **许可**：当前版本尚未指定开源许可证。
