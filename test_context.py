"""验证请求副本隔离、工具协议保留和被省略内容确实可取回。"""

import copy
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent import run_agent
from context import RequestContext
from reviewer import review_answer
from test_agent import assistant, request
from test_reviewer import recommendation
from tool_registry import Tool, ToolRegistry


def result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def output_id(content):
    return re.search(r"result_id=([0-9a-f]{64})", content).group(1)


class ContextTests(unittest.TestCase):
    def test_request_copy_preserves_protocol_and_cannot_mutate_original_history(self):
        history = [assistant(request("read", "read_file", {"name": "schedule.txt"})), result("read", "短结果")]
        original = copy.deepcopy(history)
        context = RequestContext()
        view = context.build(history)
        self.assertEqual(view, history)
        view[0]["tool_calls"][0]["function"]["name"] = "changed"
        view[1]["content"] = "changed"
        self.assertEqual(history, original)

    def test_long_output_is_bounded_and_fully_recoverable_including_unicode(self):
        content = "开头" * 150 + "中间关键事实" + "结尾" * 150
        history = [assistant(request("read", "read_file", {"name": "notes.txt"})), result("read", content)]
        original = copy.deepcopy(history)
        context = RequestContext(256)
        view = context.build(history)
        self.assertEqual(len(view[1]["content"]), 256)
        self.assertNotIn("中间关键事实", view[1]["content"])
        self.assertEqual(view[1]["tool_call_id"], "read")
        self.assertEqual(view[0], history[0])
        key = output_id(view[1]["content"])
        restored = "".join(context.read_tool_result(key, offset) for offset in range(0, len(content), 256))
        self.assertEqual(restored, content)
        self.assertEqual(context.read_tool_result(key, len(content)), "")
        self.assertEqual(history, original)
        self.assertEqual(output_id(context.build(history)[1]["content"]), key)
        self.assertLess(context.requests[0]["request_content_chars"], context.requests[0]["original_content_chars"])

    def test_cached_output_is_scoped_to_its_own_session_and_rejects_bad_offsets(self):
        context = RequestContext(256)
        key = output_id(context.build([result("id", "x" * 600)])[0]["content"])
        other = RequestContext(256)
        self.assertTrue(other.read_tool_result(key, 0).startswith("错误："))
        registry = ToolRegistry([context.result_tool()])
        for offset in (-1, True, "0", 0.5):
            with self.subTest(offset=offset):
                self.assertTrue(registry.execute(request("bad", "read_tool_result", {
                    "result_id": key, "offset": offset,
                })).startswith("错误："))
        schema = registry.schemas()[0]["function"]["parameters"]
        self.assertEqual(schema["properties"]["offset"]["type"], "integer")
        self.assertEqual(registry.execute(request("ok", "read_tool_result", {
            "result_id": key, "offset": 256,
        })), "x" * 256)

    def test_actor_can_fetch_a_fact_missing_from_the_preview(self):
        source = "x" * 256 + "关键事实：集合地点是东门。" + "y" * 400
        registry = ToolRegistry([Tool("long_notes", "读取长材料", {}, lambda: source)])

        def model(messages, tools):
            if messages[-1]["role"] == "user":
                return assistant(request("long", "long_notes", {}))
            if messages[-1]["tool_call_id"] == "long":
                self.assertNotIn("集合地点是东门", messages[-1]["content"])
                return assistant(request("chunk", "read_tool_result", {
                    "result_id": output_id(messages[-1]["content"]), "offset": 256,
                }))
            self.assertIn("集合地点是东门", messages[-1]["content"])
            return assistant(content="集合地点是东门。")

        with patch("agent.REGISTRY", registry), patch("agent.chat", side_effect=model):
            with redirect_stdout(io.StringIO()):
                report = run_agent("集合在哪里？", review=False, tool_output_chars=256)
        self.assertEqual(report["answer"], "集合地点是东门。")
        self.assertEqual(report["messages"][3]["content"], source)
        self.assertEqual(len(report["context_requests"]), report["model_requests"])
        self.assertNotIn("集合地点是东门", report["context_requests"][1]["messages"][-1]["content"])

    def test_reviewer_can_retrieve_inherited_evidence_and_keeps_the_full_record(self):
        source = "x" * 256 + "关键事实：集合地点是东门。" + "y" * 400
        evidence = [assistant(request("read", "read_file", {"name": "notes.txt"})), result("read", source)]

        def model(messages, tools):
            if len(messages) == 2:
                payload = json.loads(messages[1]["content"])
                preview = payload["tool_evidence"][1]["content"]
                self.assertNotIn("集合地点是东门", preview)
                return assistant(request("verify", "read_tool_result", {
                    "result_id": output_id(preview), "offset": 256,
                }))
            self.assertIn("集合地点是东门", messages[-1]["content"])
            return recommendation()

        with patch("reviewer.chat", side_effect=model), redirect_stdout(io.StringIO()):
            report = review_answer("集合在哪？", "在东门", evidence, tool_output_chars=256)
        self.assertEqual(report["recommendation"], "accept")
        self.assertEqual(report["tool_evidence"][-1], evidence[-1])
        self.assertEqual(report["tool_evidence"][0]["tool_calls"], evidence[0]["tool_calls"])
        self.assertEqual(len(report["context_requests"]), report["model_requests"])

    def test_limit_is_explicit_and_not_a_whole_conversation_trimmer(self):
        task = "任务要求" * 300
        context = RequestContext(256)
        self.assertEqual(context.build([{"role": "user", "content": task}])[0]["content"], task)
        for invalid in (255, True, "4000", 256.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                RequestContext(invalid)

    def test_smart_zip_archives_stale_reads_and_keeps_active_code_reads(self):
        old_hash = "a" * 64
        new_hash = "b" * 64
        keep_hash = "c" * 64
        stale_read = f"文件: a.py\nsha256: {old_hash}\n总行数: 100\n" + "\n".join(f"{i}: x = {i} # padding_line_text" for i in range(1, 45))
        active_read = f"文件: b.py\nsha256: {keep_hash}\n总行数: 100\n" + "\n".join(f"{i}: y = {i} # padding_line_text" for i in range(1, 45))
        edit_msg = f"已修改 a.py；sha256: {new_hash}；workspace_revision: 1"
        cmd_out = '{"job_id": "job-1", "workspace_revision_start": 0, "output": "' + ("报错行\n" * 800) + '"}'
        history = [
            result("r1", stale_read),
            result("r2", active_read),
            result("c1", cmd_out),
            result("c2", cmd_out),
            result("c3", cmd_out),
            result("e1", edit_msg),
            result("c4", cmd_out),
            result("c5", cmd_out),
        ]
        ctx = RequestContext(2000, smart_zip=True)
        view = ctx.build(history)
        self.assertIn("上一阶段工具输出已归档", view[0]["content"])
        self.assertEqual(view[1]["content"], active_read)
        self.assertIn("上一阶段工具输出已归档", view[2]["content"])



if __name__ == "__main__":
    unittest.main()
