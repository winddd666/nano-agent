# Nano Agent

用于课程实验与 Agent 错误分析的小型 Python 工具智能体。这个仓库固定当前实现，便于核对代码、运行配置与后续改造；源码保留原有的 AI 辅助开发标记。

本次快照标签：**`stage1-20261110`**。运行代码、测试与示例材料逐文件对应 `source_manifest.json`，发布准备过程没有修改 Agent 实现。引用实验版本时请同时记录完整 commit SHA、模型、任务提示、工具与预算；该标签不代表所有历史实验都曾运行于这一版。

## 当前能力

- 本地材料查询、文件读取与搜索；带 SHA256 检查的文件编辑。
- 文件隔离环境中的命令执行、前台/后台作业、退出码与实际输出记录。
- 只读命令并发、写命令互斥、任务内共享 `/tmp`。
- 总运行预算与可选单命令时限、取消、运行中追加用户要求。
- 独立的建议性 reviewer；上下文输出节选与原文取回，可选 smart_zip。
- 区分工具能力 `profile` 和任务提示 `task_mode`：coding 工具可以配 coding 或 QA 提示。
- 保存最终报告及 JSONL 事件，包含真实模型请求与工具结果。

具体结构见 [架构说明](docs/architecture.md)，已知问题见 [限制与待研究问题](docs/limitations.md)。

## 运行条件

已验证环境为 **Linux / Python 3.14.9**。Python 代码仅使用标准库，不需要安装 pip 依赖。coding 和 QA 工作区命令需要 `bubblewrap`（`bwrap`），并要求运行环境能够隔离命令网络；初始化无法建立该边界时会拒绝运行。当前没有验证 Windows 或 macOS。

API 接线位于 `model.py`，当前模型标识为 `deepseek-flash`，使用 DeepSeek Chat Completions。配置凭据：

```bash
export DEEPSEEK_API_KEY='your-api-key'
python3 agent.py --help
```

代码也保留了原学习环境中的本机配置回退读取逻辑；使用上述环境变量即可，无须准备其他客户端配置。仓库不包含任何凭据。

## 三种使用方式

### 查询示例材料

```bash
python3 agent.py 'Agent 实验课需要提前准备什么？标注依据。' \
  --profile materials --max-calls 20 --run-timeout-s 180
```

`sandbox/` 中是合成的学习小组材料。

### 用本地工具辅助问答

```bash
NANO_QA_WORKSPACE=$(mktemp -d)
python3 agent.py '计算 137 × 249，最后给出 Final Answer: <答案>。' \
  --profile coding --task-mode qa --workspace "$NANO_QA_WORKSPACE" \
  --max-calls 20 --run-timeout-s 180 --command-timeout-s 30
```

`profile=coding` 提供工作区与命令工具；`task_mode=qa` 选择问答执行提示与问答 reviewer，不要求修改代码或生成 diff。

### 在独立示例工作区修复代码

```bash
NANO_DEMO_WORKSPACE=$(mktemp -d)
cp -r fixtures/todo_cli/. "$NANO_DEMO_WORKSPACE/"
python3 agent.py '修复新建待办事项时的 ID 冲突，保留已有行为，运行项目测试。' \
  --profile coding --workspace "$NANO_DEMO_WORKSPACE" \
  --max-calls 40 --run-timeout-s 300 --command-timeout-s 60
```

`fixtures/todo_cli` 是故意保留 ID 分配缺陷的练习材料，它自己的初始测试预期会失败；它不属于下方的 Agent 单元测试集合。

### 常用开关与输出

- `--interactive`：接受运行中追加要求；`/now` 请求尽快处理，`/bg` 或 Ctrl+B 将当前前台等待转为后台。
- `--no-review`：关闭建议性评审；`--no-stream`：关闭模型流式输出。
- `--run-timeout-s`：本次 Agent 总预算；`--command-timeout-s`：单命令可选上限；`--max-jobs`：题内只读命令并发限制。
- `--criterion`：替换默认验收条件，可重复指定。

省略运行/命令时限时，CLI 不自动添加时限。以上示例中的时限是使用参数，不是对所有实验有效的最佳预算结论。

运行结果写入 `runs/`，该目录已在 `.gitignore` 中排除。真实运行记录可能包含输入材料与代码，应按自己的发布范围管理。

## 无模型测试

在仓库根目录运行：

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

这些测试使用受控模型响应和实际子进程，验证工具协议、状态更新、评审路由、取消与命令执行等行为，不调用付费模型 API，也不证明真实模型一定正确。版本的实际验证结果记录在 [测试记录](docs/validation.md)。

## 与课程评测记录的关系

研究使用 SWE-bench Verified、Aider Polyglot 与 GAIA 的选定任务。此公开仓库包含 Agent 实现与合成示例，不附带 benchmark 缓存、第三方仓库副本、用户原始轨迹或个人作业全文。部分汇总与解释边界见 [实验记录说明](docs/experiment-record.md)。

完整课程证据另行归档；当前快照与历史实验之间通过源码哈希、配置和真实请求区分。repo 的后续提交也不会自动代表过去实验的运行版本。

## 许可

本次公开快照尚未指定开源许可证。
