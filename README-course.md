# Nano Agent 课程实验与版本演进说明

本文档面向课程实验核查、Agent 错误机制分析（Error Study）与多阶段受控消融复现，重点说明从初始基线到当前版本的**问题动机、运行契约差异与对照实验开关**。日常工程使用与模块二次扩展说明见主文档 [README.md](README.md)。

**相关文档**：[项目主页（README）](README.md) · [实验记录与评分汇总](docs/experiment-record.md) · [架构说明](docs/architecture.md) · [发布验证记录](docs/validation.md) · [已知限制与待研究问题](docs/limitations.md)

---

## 1. 阶段快照与演进总览

研究围绕 SWE-bench Verified、Aider Polyglot 与 GAIA 选定任务上的真实失败轨迹展开，按“错误定位 → 受控改造 → 正交消融 → 长尾收口”分为三个阶段（各阶段实验统计与解释边界详见 [docs/experiment-record.md](docs/experiment-record.md)）：

| 阶段版本 | Git 标签 / Commit | 针对的核心失败机制 | 关键机制改动 | 验证与评测记录 |
|---|---|---|---|---|
| **Stage 1 · 基线归档** | `stage1-20261110` (`93ee719`) | 早期总时限漏算、隐藏 `120s` 硬超时盲杀、GAIA 误套 coding 提示 | 修复任务总预算统计；解耦工具能力 `profile` 与任务提示 `task_mode=qa`；建立独立只读 Reviewer 与长输出节选基线 | 101 项无模型测试；36 题基线 Error Study 与 GAIA 双提示各 10 轮对照 |
| **Stage 2 · 有界诊断执行 (`v4`)** | `stage2-v4-20261111` (`71973b2`) | 去掉短超时后单条慢命令吞噬整题预算；硬超时强杀丢失输出；后台 `delta_chars=0` 无法区分正常计算与死循环 | **`M1` 有界前台调度**：前台超时自动转后台（不杀进程）、有界增量等待、只读并发放行编辑、写作业交付门禁；**`M2` 带外实时诊断**：`SIGUSR2` 实时采样 Python 栈与 CPU 活性，停止先发 `SIGINT` 捕获调用栈 | 108 项无模型测试；104 次受控运行（36 题主对比 + SWE 16 题双消融组），官方通过且正常交付 `28/36 → 33/36`，平均耗时 `519s → 207s` |
| **Stage 3 · 减摩与收敛硬化 (`v5.0`–`v5.2`)** | `stage3-v5.1-20261112` (`2c53dc5`)；当前归档 `v5.2` (`34c2bb2`) | Reviewer 3 轮预算截断（`limit_reached`）与 JSON 围栏报错；工作区编辑高频报错；多层 `subprocess` 父栈遮蔽内层工作栈；减摩后暴露的 Actor 步数盲区 | **`v5.0`**：Reviewer 预置 `workspace_snapshot` + 末轮强制收尾 + 宽容解析，`/work` 路径规整与 `SHA256`/行号提示，按 `<pid>` 穿透叶节点栈与命令 `hint`；**`v5.1`**：后向 JSON 扫描 + Actor 低步数提醒与终轮强制合成；**`v5.2`**：`SIGUSR2` 归属 inode/NSpid 校验、流式增量 UTF-8 解码与 FIFO 守卫 | 117 项无模型测试；`v5.0` 完成 36 题实测与官方回评（Reviewer `limit_reached` 降至 `0/34`），`v5.1`/`v5.2` 完成长尾边界单测与沙箱契约收口（未宣称重跑新分数） |

---

## 2. 三阶段运行契约差异详解

### 2.1 Stage 1 → Stage 2 (`v4`：`M1 + M2`)

Stage 1 基线去掉早期隐藏的 `120s` 命令硬上限后，暴露出**“前台同步长阻塞吞噬整题预算”**与**“短硬超时黑盒强杀丢失缓冲输出”**的两难困境。Stage 2 在不新增工具名称、不改变快命令返回结构的前提下引入两层正交机制：

- **从同步死等改为有界让出与读写分离（`M1`）**：
  - **Stage 1**：前台 `run_command` 一直同步阻塞至子进程退出或触顶强杀；任何后台作业运行期间均禁止 `edit_file` / `create_file`。
  - **Stage 2 (`M1`)**：配置 `--foreground-yield-s` 后，前台命令超过观察窗口**不会终止子进程**，而是原地转为后台任务并返回 `job_id` 与已有输出；配合 `--wait-timeout-s` 有界增量等待与 `--max-command-timeout-s` 超限静默钳位，交还模型决策权。启用 `--allow-edit-during-readonly` 时，后台只读测试不阻塞代码编辑；若有在途 `write` 命令，则拦截候选评审与最终交付以保证 `diff` 一致。
- **从字符增量猜测改为带外栈采样与优雅展开（`M2`）**：
  - **Stage 1**：后台任务仅提供输出字符增量 `delta_chars`，静默高 CPU 计算与死循环在模型视角下无法区分，强杀时常无 traceback。
  - **Stage 2 (`M2`)**：沙箱内只读挂载 `sitecustomize` 注册 `SIGUSR2`（与 `/work` 隔离，零 `diff` 污染）。任务运行期间（`--live-diag`）先核查 `/proc/<pid>/status` 信号掩码（原生 Go/C++ 二进制安全跳过），再无损采集实时调用栈与 CPU 活性；超时或取消时（`--stop-with-sigint`）先向内层进程发 `SIGINT` 触发 `KeyboardInterrupt` 行号输出，再按宽限期回收。

### 2.2 Stage 2 (`v4`) → Stage 3 (`v5.0` → `v5.2`)

对 Stage 2 的 104 条轨迹逐事件审计后，定位出三类高频工程损耗并在 `v5` 系列中逐层收口：

- **消除 Reviewer 预算悬崖与 Actor 步数盲区（`--ergonomic-v5`）**：
  - **Reviewer 侧**：`v4` 中大量评审首轮调用 `get_diff`，随后在第 3 轮耗尽预算返回 `limit_reached` 或因 Markdown 代码围栏报格式错误。`v5` 预注入 `workspace_snapshot`（含修订号、`diff` 摘要与文件列表），在评审末轮切换无工具模式强制输出结论，异常时降级直通；`v5.1` 进一步改为从后向前扫描 JSON 对象，避免前置推理中的内联 `{}` 误匹配。
  - **Actor 侧**：`v5.0` 降低文件创建阻力后，暴露出部分任务在补丁已正确的情况下反复自测直至耗尽 `max_calls` 而未触发交付。`v5.1` 新增低剩余步数一次性收敛提醒、最后一轮无工具强制合成交付，以及零剩余步数下的评审建议直通。
- **消除工具协议摩擦与嵌套子进程栈遮蔽**：
  - **工作区与工具层**：默认支持 `/work/...` 与 `./...` 路径归一化、`create_file(..., overwrite=true)`、`edit_file` 冲突时直接回传最新 `SHA256` 与相似/多处匹配行号、轻微畸形 JSON 参数恢复，以及 `pytest` / `git` / 只读挂载 `EROFS` 单行替代提示（`hint`）。
  - **多进程诊断与边界硬化**：`v5.0` 将诊断栈改为按 `<job_id>.<pid>.stack` 分文件记录，穿透 `runtests.py -> subprocess` 采集活跃叶节点 Python 栈；`v5.2` 补齐信号发送前的 fd `(dev, ino)` + `NSpid` 归属核验、运行中增量 UTF-8 解码与特殊文件（如 FIFO）非阻塞校验。

---

## 3. 四组受控实验配置的 CLI 开关对照

各阶段改造均以正交参数保留。除使用 `git checkout <tag>` 检出历史精确快照外，也可直接在当前代码上通过 CLI 参数切换四组受控实验契约：

| 对照配置 | 在基础参数（`--profile coding --workspace "$WS"`）上启用的核心开关 |
|---|---|
| **Stage 1 / Stage 2 Baseline** | 不传有界调度、`live-diag` 与 `ergonomic-v5` 开关（保持同步阻塞与 3 轮默认评审） |
| **Stage 2 Ablation B（仅 `M1`）** | `--foreground-yield-s 25 --wait-timeout-s 30 --allow-edit-during-readonly` |
| **Stage 2 Full（`v4`：`M1 + M2`）** | 上行 `M1` 开关 + `--live-diag --stop-with-sigint` |
| **Stage 3 Full（`v5`：全套增强）** | 上行 `v4` 开关 + `--ergonomic-v5 --max-review-calls 6` |

### 最小对照验证示例

```bash
# 1. 无模型回归测试（无需 API Key，验证 117 项运行协议与 bwrap 子进程契约）
python3 -m unittest discover -s . -p 'test_*.py' -v

# 2. 在内置合成缺陷练习项目上运行 Stage 3 Full 配置（需 Linux + bwrap + DeepSeek API Key）
export DEEPSEEK_API_KEY='your-api-key'
NANO_DEMO_WS=$(mktemp -d) && cp -r fixtures/todo_cli/. "$NANO_DEMO_WS/"

python3 agent.py '修复新建待办事项时的 ID 冲突，保留已有行为，运行项目测试。' \
  --profile coding --workspace "$NANO_DEMO_WS" \
  --max-calls 40 --run-timeout-s 300 --command-timeout-s 180 --max-command-timeout-s 300 \
  --foreground-yield-s 25 --wait-timeout-s 30 --allow-edit-during-readonly \
  --live-diag --stop-with-sigint --ergonomic-v5 --max-review-calls 6
```

> 说明：`fixtures/todo_cli` 是故意保留 ID 分配缺陷的练习材料，初始测试预期失败，不属于 Agent 单元测试集合；若复现 Stage 1 修正后的通用问答身份（如 GAIA 文本题模式），将 `--profile coding` 配合 `--task-mode qa` 使用即可。全部 CLI 参数定义见 [README.md](README.md)。

---

## 4. 公开范围与实验引用边界

1. **受控源码清单**：仓库内 34 个运行源码、测试与合成示例文件的逐文件 `SHA256` 记录于 [`source_manifest.json`](source_manifest.json)（当前对应 `stage3-v5.2-20261112`，与本地评测 runtime 逐文件一致）；发布准备过程未修改 Agent 实现，源码保留原有的 AI 辅助开发标记。
2. **未公开材料边界**：本公开仓库仅包含 Agent 实现、单元测试与合成示例（`sandbox/`、`fixtures/todo_cli/`），**不附带**第三方 benchmark 数据集缓存、外部仓库副本、原始运行轨迹或个人作业文稿。无模型测试仅验证运行协议与代码契约，不构成真实模型能力或外部 benchmark 成绩的证明。
3. **版本引用要求**：引用本仓库开展或对比实验时，请同时记录完整 `commit SHA`、模型标识、任务提示、启用的工具/诊断开关以及总时限/步数预算。当前分支与各阶段标签仅固定对应时刻的代码实现；仓库后续的功能扩展提交也不会追溯代表过去实验的运行版本。
