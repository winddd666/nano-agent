"""从原始历史生成模型请求视图；长工具输出节选后仍可按位置取回。"""

import re
import json
from copy import deepcopy
from hashlib import sha256

from tool_registry import Parameter, Tool


DEFAULT_TOOL_OUTPUT_CHARS = 4000
_CODE_DEF_RE = re.compile(r"^\s*(\d+:\s*(?:class|def|async\s+def|func|type|struct)\s+[A-Za-z_][A-Za-z0-9_]*[^\n]{0,60})", re.M)
_ERR_LINE_RE = re.compile(r"^.*(?:Traceback|Error:|Exception:|FAIL:|FAILED|AssertionError|RecursionError|panic:).*$", re.M)
_READ_HEADER_RE = re.compile(r"^文件:\s*(.+?)\nsha256:\s*([0-9a-f]{64})\n总行数:\s*\d+\n")
_LINE_NO_RE = re.compile(r"^(\d+):", re.M)
_EDIT_RESULT_RE = re.compile(r"^已(?:修改|创建)\s+(.+?)；sha256:\s*([0-9a-f]{64})；workspace_revision:\s*(\d+)")
_JOB_REV_RE = re.compile(r'"workspace_revision_(?:end|start)":\s*(\d+)')


def _parse_read_meta(content: str):
    """提取 read_file 输出的 (path, sha256, start_line, end_line)；非 read_file 返回 None。"""
    m = _READ_HEADER_RE.match(content)
    if not m:
        return None
    path, digest = m.group(1).strip(), m.group(2)
    body = content[m.end():]
    first_m = _LINE_NO_RE.search(body[:256])
    last_nums = _LINE_NO_RE.findall(body[-512:])
    start_line = int(first_m.group(1)) if first_m else 0
    end_line = int(last_nums[-1]) if last_nums else start_line
    return path, digest, start_line, end_line


def _summarize_omitted_middle(middle: str, max_chars: int = 480) -> str:
    """Zero-LLM Zip：在 <1ms 内从被省略的中间段落提取符号行号骨架或关键报错行。"""
    if "\n" not in middle or max_chars < 80:
        return "\n[…中间已省略…]\n"
    hints = []
    defs = _CODE_DEF_RE.findall(middle)
    if defs:
        hints.append("省略区符号定义: " + " | ".join(d.strip() for d in defs[:10]))
    errs = [line.strip()[:120] for line in _ERR_LINE_RE.findall(middle) if line.strip()]
    if errs:
        dedup_errs = list(dict.fromkeys(errs))[:5]
        hints.append("省略区异常/报错: " + " ; ".join(dedup_errs))
    # 检测连续重复行（如递归调用栈）
    lines = middle.splitlines()
    if len(lines) > 30:
        unique_ratio = len(set(lines)) / len(lines)
        if unique_ratio < 0.25:
            hints.append(f"省略区含大量重复行（共 {len(lines)} 行，去重后仅 {len(set(lines))} 种）")
    if not hints:
        return "\n[…中间已省略…]\n"
    body = "；".join(hints)
    if len(body) > max_chars - 24:
        body = body[: max_chars - 27] + "..."
    return f"\n[…中间已省略（{body}）…]\n"


class RequestContext:
    def __init__(self, tool_output_chars=DEFAULT_TOOL_OUTPUT_CHARS, *, smart_zip=False):
        if type(tool_output_chars) is not int or tool_output_chars < 256:
            raise ValueError("tool_output_chars 必须是至少 256 的整数")
        self.tool_output_chars = tool_output_chars
        self.smart_zip = bool(smart_zip)
        self._outputs = {}
        self._archived_cutoff = 0
        self._archived_revision = 0
        self.requests = []

    def build(self, history, *, record=True):
        """只改请求副本；保留消息顺序、工具请求及结果 id。"""
        messages = deepcopy(history)
        previews = []

        latest_file_hash = {}
        read_records = {}
        max_revision = self._archived_revision

        if self.smart_zip:
            for idx, m in enumerate(messages):
                c = m.get("content")
                if m.get("role") != "tool" or not isinstance(c, str):
                    continue
                em = _EDIT_RESULT_RE.match(c)
                if em:
                    latest_file_hash[em.group(1).strip()] = em.group(2)
                    max_revision = max(max_revision, int(em.group(3)))
                    continue
                rm = _parse_read_meta(c)
                if rm:
                    path, digest, s_line, e_line = rm
                    latest_file_hash[path] = digest
                    read_records[idx] = (path, digest, s_line, e_line)
                    continue
                for jm in _JOB_REV_RE.finditer(c[:600]):
                    max_revision = max(max_revision, int(jm.group(1)))

            active_tools = [
                (idx, len(m["content"]))
                for idx, m in enumerate(messages)
                if idx >= self._archived_cutoff and m.get("role") == "tool" and isinstance(m.get("content"), str)
            ]
            active_chars = sum(length for _, length in active_tools)
            revision_advanced = max_revision > self._archived_revision
            # 阶段性触发：优先在 workspace_revision 推进且累计满一定规模时归档；纯探索阶段放宽门槛保护 KV Cache
            if (revision_advanced and len(active_tools) >= 6 and active_chars >= 14000) or (
                len(active_tools) >= 10 and active_chars >= 24000
            ):
                self._archived_cutoff = active_tools[-3][0]
                self._archived_revision = max_revision

        for idx, message in enumerate(messages):
            content = message.get("content")
            if message.get("role") != "tool" or not isinstance(content, str):
                continue
            in_archived_zone = self.smart_zip and idx < self._archived_cutoff
            is_archived_stage = False
            if in_archived_zone:
                if idx in read_records:
                    path, digest, s_line, e_line = read_records[idx]
                    hash_stale = latest_file_hash.get(path) != digest
                    range_superseded = any(
                        other_idx > idx
                        and other_path == path
                        and other_digest == digest
                        and other_s <= s_line
                        and other_e >= e_line
                        for other_idx, (other_path, other_digest, other_s, other_e) in read_records.items()
                    )
                    # 只有已被后续修改使 sha256 过期、或被后续更大范围读取覆盖的 read_file 才归档折叠
                    is_archived_stage = hash_stale or range_superseded
                else:
                    is_archived_stage = True
            limit = min(self.tool_output_chars, 500) if is_archived_stage else self.tool_output_chars
            if len(content) <= limit:
                continue
            result_id = sha256(content.encode("utf-8")).hexdigest()
            self._outputs[result_id] = content
            visible_content = content
            # Render command evidence before trimming. JSON escaping otherwise
            # hides line boundaries, while argv/PID/timestamps consume the budget.
            try:
                job = json.loads(content)
            except (ValueError, TypeError):
                job = None
            if isinstance(job, dict) and "job_id" in job and isinstance(job.get("output"), str):
                keys = ("job_id", "status", "exit_code", "reason", "workspace_revision_start",
                        "workspace_revision_end", "stable_workspace", "timeout_s", "elapsed_s")
                metadata = {key: job[key] for key in keys if key in job}
                visible_content = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n实际输出：\n" + job["output"]
            if is_archived_stage:
                header = (
                    f"[上一阶段工具输出已归档，原文共 {len(content)} 字符；result_id={result_id}。"
                    "如需重查细节请调用 read_tool_result(result_id, offset)。]\n"
                )
            else:
                header = (
                    f"[工具输出节选，原文共 {len(content)} 字符；result_id={result_id}。"
                    "中间内容未展示，请用 read_tool_result(result_id, offset) 取回原文，"
                    "不要把省略理解为没有相关内容。]\n"
                )
            if self.smart_zip:
                prelim_avail = max(64, limit - len(header) - 20)
                h0, t0 = prelim_avail // 2, prelim_avail - prelim_avail // 2
                separator = _summarize_omitted_middle(visible_content[h0 : len(visible_content) - t0], max_chars=min(480, limit // 3))
            else:
                separator = "\n[…中间已省略…]\n"
            available = max(32, limit - len(header) - len(separator))
            head, tail = available // 2, available - available // 2
            if len(header) + len(visible_content) <= limit:
                message["content"] = header + visible_content
            else:
                message["content"] = header + visible_content[:head] + separator + visible_content[-tail:]
            previews.append({"tool_call_id": message["tool_call_id"], "result_id": result_id,
                             "original_chars": len(content), "visible_chars": len(message["content"]),
                             "archived_stage": is_archived_stage})
        if record:
            self.requests.append({
                "messages": messages, "tool_previews": previews,
                "original_content_chars": self._content_chars(history),
                "request_content_chars": self._content_chars(messages),
            })
        return messages

    @staticmethod
    def _content_chars(messages):
        return sum(len(m["content"]) for m in messages if isinstance(m.get("content"), str))

    def read_tool_result(self, result_id, offset):
        """按字符位置读取本次会话已缓存的结果，不重新执行原工具。"""
        if type(offset) is not int or offset < 0:
            return "错误：offset 必须是从 0 开始的非负整数。"
        content = self._outputs.get(result_id)
        if content is None:
            return "错误：本次会话没有这个 result_id。"
        return content[offset:offset + self.tool_output_chars]

    def result_tool(self):
        return Tool(
            name="read_tool_result",
            description=(
                "按位置读取此前被节选的工具输出原文，不重新执行原工具。"
                f"每次至多返回 {self.tool_output_chars} 个字符，下一段 offset 加此数；"
                "返回空字符串表示已到末尾。只能读取本次会话已缓存的结果。"
            ),
            parameters={
                "result_id": Parameter("工具输出节选标记中的完整 result_id。"),
                "offset": Parameter("从 0 开始的字符偏移，例如 0 表示开头。", "integer"),
            },
            handler=self.read_tool_result,
            concurrency="shared",
        )
