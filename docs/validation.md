# 发布验证

验证日期：2026-11-12。源码快照与 `source_manifest.json`（`stage3-v5.1-20261112`）对应；测试使用受控模型与真实 `bwrap` 子进程，不调用付费模型 API。

| 检查 | 实际结果 |
|---|---|
| 环境 | Linux，Python 3.14.9，bubblewrap 可用 |
| 根目录完整单元测试 | 115 项全部通过，9.0 秒 |
| `python3 agent.py --help` | 退出码 0 |
| 源码一致性 | 34 个运行代码、测试及合成材料文件与 Stage 3 (`v5.1`) runtime 逐文件 SHA256 一致 |
| 文档相对链接 | 已检查，目标文件均存在 |
| 发布内容 | 运行代码、测试、合成示例、说明与哈希清单；不包含凭据、原始运行记录、benchmark 缓存 |

测试命令：

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

完整无模型测试输出见 [validation-tests.txt](validation-tests.txt)。上述测试验证运行协议与实现行为，不能作为真实模型能力、官方 benchmark 成绩或安全边界完整性的证明。
