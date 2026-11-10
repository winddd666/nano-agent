"""验证工具扩展会同时进入模型声明和实际执行路径。"""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from agent import run_agent
from test_agent import assistant, request
from tool_registry import Parameter, Tool, ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.handler = Mock(return_value="新工具的真实结果")
        self.tool = Tool("lookup", "查询材料", {"query": Parameter("查询内容")}, self.handler)
        self.registry = ToolRegistry([self.tool])

    def test_new_registration_is_visible_and_executable_in_agent(self):
        def model(messages, tools):
            self.assertEqual(tools[0]["function"]["name"], "lookup")
            self.assertEqual(tools[0]["function"]["parameters"]["required"], ["query"])
            if messages[-1]["role"] == "user":
                return assistant(request("lookup-1", "lookup", {"query": "课程"}))
            self.assertEqual(messages[-1]["tool_call_id"], "lookup-1")
            self.assertEqual(messages[-1]["content"], "新工具的真实结果")
            return assistant(content="查询结束")

        with patch("agent.REGISTRY", self.registry), patch("agent.chat", side_effect=model):
            with redirect_stdout(io.StringIO()):
                report = run_agent("查询课程", review=False)
        self.assertEqual(report["status"], "answered")
        self.handler.assert_called_once_with(query="课程")

    def test_invalid_arguments_never_reach_handler(self):
        for args in ({}, {"query": "课程", "extra": "多余"}, {"query": 1}, []):
            with self.subTest(args=args):
                result = self.registry.execute(request("bad", "lookup", args))
                self.assertTrue(result.startswith("错误："))
        self.handler.assert_not_called()

    def test_unknown_tool_cannot_dispatch(self):
        result = self.registry.execute(request("unknown", "not_registered", {}))
        self.assertTrue(result.startswith("错误："))
        self.handler.assert_not_called()

    def test_duplicate_names_are_rejected_instead_of_overwriting_handler(self):
        with self.assertRaises(ValueError):
            ToolRegistry([self.tool, Tool("lookup", "另一个实现", {}, Mock())])

    def test_request_schema_mutation_cannot_change_execution_validation(self):
        schema = self.registry.schemas()[0]["function"]["parameters"]
        schema["properties"].clear()
        schema["required"].clear()
        result = self.registry.execute(request("missing", "lookup", {}))
        self.assertTrue(result.startswith("错误："))
        self.assertEqual(self.registry.schemas()[0]["function"]["parameters"]["required"], ["query"])
        self.handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
