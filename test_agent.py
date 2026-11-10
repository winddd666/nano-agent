"""用受控模型响应验证协议；这些测试不证明真实模型会正确完成任务。"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent import execute_tool, run_agent


def request(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def assistant(*calls, content=""):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_multiple_tools_and_history_before_next_model_request(self):
        def model(messages, tools):
            replies = [m for m in messages if m["role"] == "tool"]
            if not replies:
                return assistant(request("list", "list_files", {}))
            if len(replies) == 1:
                self.assertEqual(replies[0]["tool_call_id"], "list")
                self.assertIn("schedule.txt", replies[0]["content"])
                return assistant(
                    request("schedule", "read_file", {"name": "schedule.txt"}),
                    request("contact", "read_file", {"name": "contact.txt"}),
                )
            self.assertEqual([r["tool_call_id"] for r in replies], ["list", "schedule", "contact"])
            self.assertIn("Agent 实验课", replies[1]["content"])
            self.assertIn("Agent 实验答疑", replies[2]["content"])
            self.assertEqual(messages[1]["content"], "整理课程安排")
            return assistant(content="这是受控模型的测试回答。")

        with patch("agent.chat", side_effect=model) as mocked:
            report = run_agent("整理课程安排", review=False)
        self.assertEqual(report["status"], "answered")
        self.assertEqual(mocked.call_count, 3)

    def test_tool_error_is_returned_and_model_can_continue(self):
        def model(messages, tools):
            if messages[-1]["role"] == "user":
                return assistant(request("missing", "read_file", {"name": "missing.txt"}))
            self.assertEqual(messages[-1]["tool_call_id"], "missing")
            self.assertTrue(messages[-1]["content"].startswith("错误："))
            return assistant(content="没有取得这个文件的内容。")

        with patch("agent.chat", side_effect=model):
            report = run_agent("读取不存在的文件", review=False)
        self.assertEqual(report["status"], "answered")
        self.assertEqual(report["model_requests"], 2)

    def test_limit_reports_unfinished_without_an_extra_request(self):
        with patch("agent.chat", return_value=assistant(request("list", "list_files", {}))) as model:
            report = run_agent("读取材料", max_calls=1, review=False)
        self.assertEqual(report["status"], "limit_reached")
        self.assertEqual(report["messages"][-1]["role"], "tool")
        self.assertEqual(model.call_count, 1)

    def test_invalid_tool_requests_return_errors(self):
        calls = [
            request("unknown", "unregistered", {}),
            request("extra", "list_files", {"extra": 1}),
            request("missing_argument", "read_file", {}),
            request("wrong_type", "read_file", {"name": 123}),
            request("array", "read_file", []),
            {"function": {"name": "read_file", "arguments": "{"}},
        ]
        for call in calls:
            with self.subTest(call=call):
                self.assertTrue(execute_tool(call).startswith("错误："))

    def test_model_failure_stops_without_a_fabricated_answer(self):
        with patch("agent.chat", side_effect=RuntimeError("测试网络故障")):
            report = run_agent("读取材料", review=False)
        self.assertEqual(report["status"], "runtime_error")
        self.assertFalse(any(m["role"] == "assistant" for m in report["messages"]))

    def test_empty_response_is_not_an_answer(self):
        with patch("agent.chat", return_value=assistant()):
            report = run_agent("读取材料", review=False)
        self.assertEqual(report["status"], "empty_response")


if __name__ == "__main__":
    unittest.main()
