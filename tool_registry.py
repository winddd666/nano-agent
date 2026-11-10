"""工具声明与执行的共同入口；支持必填字符串和整数参数。"""

import json
from dataclasses import dataclass
from typing import Callable

from tool import list_files, read_file


@dataclass(frozen=True)
class Parameter:
    description: str
    kind: str = "string"
    required: bool = True

    def __post_init__(self):
        if self.kind not in ("string", "integer", "string_array", "string_map"):
            raise ValueError(f"不支持的参数类型：{self.kind}")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    # 声明的参数均为必填；不是通用 JSON Schema 校验器。
    parameters: dict[str, Parameter]
    handler: Callable[..., str]
    concurrency: str = "exclusive"
    interrupt: str = "wait"

    def __post_init__(self):
        if self.concurrency not in ("shared", "exclusive", "control"):
            raise ValueError("concurrency 必须是 shared、exclusive 或 control")
        if self.interrupt not in ("wait", "cancel", "background"):
            raise ValueError("interrupt 必须是 wait、cancel 或 background")
        if self.interrupt == "cancel" and self.concurrency != "shared":
            raise ValueError("协作取消工具必须声明 shared，并接收 cancellation 参数")
        if self.interrupt == "cancel" and "cancellation" in self.parameters:
            raise ValueError("cancellation 是运行时保留参数")
        if self.interrupt == "background" and self.concurrency != "exclusive":
            raise ValueError("后台交接工具必须由主循环独占分发")

    def schema(self):
        """从注册信息生成模型侧定义，每次返回独立的字典。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        name: ({"type": "array", "items": {"type": "string"}, "description": parameter.description}
                               if parameter.kind == "string_array" else
                               {"type": "object", "additionalProperties": {"type": "string"}, "description": parameter.description}
                               if parameter.kind == "string_map" else
                               {"type": parameter.kind, "description": parameter.description})
                        for name, parameter in self.parameters.items()
                    },
                    "required": [name for name, spec in self.parameters.items() if spec.required],
                    "additionalProperties": False,
                },
            },
        }


@dataclass(frozen=True)
class PreparedTool:
    tool: Tool
    arguments: dict

    def invoke(self, cancellation=None):
        if self.tool.interrupt == "cancel":
            return self.tool.handler(**self.arguments, cancellation=cancellation)
        return self.tool.handler(**self.arguments)


class ToolRegistry:
    def __init__(self, tools):
        self._tools = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"工具重复注册：{tool.name}")
            self._tools[tool.name] = tool

    def schemas(self):
        return [tool.schema() for tool in self._tools.values()]

    def select(self, *names):
        """显式选择工具能力；评审者只取得指定的只读文件工具。"""
        return ToolRegistry(self._tools[name] for name in names)

    def with_tools(self, *tools):
        return ToolRegistry([*self._tools.values(), *tools])

    def prepare(self, call):
        """解析请求并验证声明的参数；参数错误回填给模型。"""
        try:
            function = call["function"]
            name = function["name"]
            args = json.loads(function["arguments"])
        except (KeyError, TypeError, json.JSONDecodeError):
            return "错误：工具请求格式不正确，arguments 必须是 JSON 字符串。"

        if not isinstance(name, str):
            return "错误：工具名称必须是字符串。"
        if not isinstance(args, dict):
            return "错误：工具参数必须是 JSON 对象。"
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：没有注册工具 {name}。"
        required = {name for name, spec in tool.parameters.items() if spec.required}
        if not required <= set(args) or not set(args) <= set(tool.parameters):
            expected = ", ".join(tool.parameters) or "无参数"
            return f"错误：{name} 参数不匹配，要求：{expected}。"
        for parameter, spec in tool.parameters.items():
            if parameter not in args:
                continue
            value = args[parameter]
            if spec.kind == "string" and not isinstance(value, str):
                return f"错误：{name} 的 {parameter} 参数必须是字符串。"
            if spec.kind == "integer" and type(value) is not int:
                return f"错误：{name} 的 {parameter} 参数必须是整数。"
            if spec.kind == "string_array" and (not isinstance(value, list) or
                    any(not isinstance(item, str) for item in value)):
                return f"错误：{name} 的 {parameter} 参数必须是字符串数组。"
            if spec.kind == "string_map" and (not isinstance(value, dict) or
                    any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items())):
                return f"错误：{name} 的 {parameter} 参数必须是字符串映射。"
        return PreparedTool(tool, args)

    def execute(self, call):
        prepared = self.prepare(call)
        if isinstance(prepared, str):
            return prepared
        if prepared.tool.interrupt == "cancel":
            from threading import Event
            return prepared.invoke(Event())
        return prepared.invoke()


REGISTRY = ToolRegistry([
    Tool(
        name="list_files",
        description=(
            "列出 sandbox 目录中的可见条目名称，用于发现可读取的文件。"
            "成功时返回名称列表文本，失败时返回错误信息。"
        ),
        parameters={},
        handler=list_files,
        concurrency="shared",
    ),
    Tool(
        name="read_file",
        description=(
            "读取 sandbox 目录内的普通文本文件。"
            "成功时返回文件正文，失败时返回错误信息。"
            "只能读取 sandbox 目录内的文件。"
        ),
        parameters={"name": Parameter("相对于 sandbox 的文件名，例如 schedule.txt。")},
        handler=read_file,
        concurrency="shared",
    ),
])
