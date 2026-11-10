"""兼容首次请求和观察脚本；工具定义由注册表生成。"""

from tool_registry import REGISTRY

TOOLS = REGISTRY.schemas()
