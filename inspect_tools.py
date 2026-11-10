"""AI 准备的观察入口：只请求一次并展示响应，不执行工具。"""

import json
import sys

from model import chat
from tool_spec import TOOLS


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or "请先列出 sandbox 目录中的文件。"
    messages = [{"role": "user", "content": task}]
    try:
        message = chat(messages, tools=TOOLS)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(message, ensure_ascii=False, indent=2))
