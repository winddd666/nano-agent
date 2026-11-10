import subprocess
from pathlib import Path


SANDBOX = Path(__file__).resolve().parent / "sandbox"


def _run(command):
    """执行命令，把成功输出和失败信息都整理成工具可返回的文本。"""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return "错误：命令执行超过 5 秒。"
    except OSError as exc:
        return f"错误：无法执行命令（{exc.strerror}）。"

    if result.returncode == 0:
        return result.stdout
    return f"错误：{result.stderr.strip() or f'命令退出码为 {result.returncode}'}"


def list_files():
    return _run(["ls", "-1", str(SANDBOX)])


def read_file(name):
    if not isinstance(name, str):
        return "错误：文件名必须是字符串。"

    try:
        fname = (SANDBOX / name).resolve()
    except (OSError, RuntimeError, ValueError):
        return "错误：无法解析文件路径。"

    if not fname.is_relative_to(SANDBOX):
        return "错误：只能读取 sandbox 目录内的文件。"
    if not fname.is_file():
        return f"错误：文件不存在或不是普通文件：{name}"

    return _run(["cat", "--", str(fname)])
