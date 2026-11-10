"""AI 准备的连通检查：仅一次问候，不实现 Agent 框架。"""

import json
import sys

from model import chat


if __name__ == "__main__":
    try:
        message = chat([{"role": "user", "content": "请只回复：连接成功。"}])
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(message, ensure_ascii=False, indent=2))
