"""Coding profile tools and a file-limited project command environment."""

import json
import os
import re
import shutil
import socket
import subprocess

from tool_registry import Parameter, Tool, ToolRegistry
from control import RunTimedOut


class CodingTools:
    def __init__(self, workspace, jobs, *, check_argv=None):
        self.workspace, self.jobs = workspace, jobs
        self.check_argv = check_argv
        if shutil.which("bwrap") is None:
            raise RuntimeError("coding profile 需要 bwrap，当前环境未安装")
        probe = ["bwrap", "--unshare-net", "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
                 "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64", "--", "/usr/bin/python3", "-c", "pass"]
        try:
            isolated = subprocess.run(probe, capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            isolated = False
        if isolated:
            self.network_boundary = "bwrap network namespace"
        else:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except PermissionError:
                self.network_boundary = "host AF_INET denied"
            else:
                sock.close()
                raise RuntimeError("无法隔离项目命令的网络访问")
        self.jobs.completion_hook = self._completed

    def _completed(self, record):
        if record.get("label") not in ("command", "project_check"):
            return
        record["workspace_revision_end"] = self.workspace.refresh()
        record["workspace_changed"] = record["workspace_revision_end"] != record["workspace_revision_start"]
        record["stable_workspace"] = not record["workspace_changed"]

    def _busy(self):
        self.jobs.poll()
        return any(record["status"] == "running" and record["label"] in ("command", "project_check")
                   for record in self.jobs.records.values())

    def edit_file(self, name, expected_hash, old_text, new_text):
        if self._busy():
            return "错误：项目命令仍在运行，暂不能编辑"
        return self.workspace.edit_file(name, expected_hash, old_text, new_text)

    def create_file(self, name, content):
        if self._busy():
            return "错误：项目命令仍在运行，暂不能创建文件"
        return self.workspace.create_file(name, content)

    def _command(self, argv, cwd, env, mode, label, access="write", timeout_s=None):
        if not isinstance(argv, list) or not argv or len(argv) > 64 or any(
                not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096 for item in argv):
            return "错误：argv 必须是 1 到 64 个非空字符串"
        if mode not in ("foreground", "background"):
            return "错误：mode 只能是 foreground 或 background"
        if access not in ("read_only", "write"):
            return "错误：access 只能是 read_only 或 write"
        if timeout_s is not None and (type(timeout_s) is not int or timeout_s <= 0):
            return "错误：timeout_s 必须为正整数秒数；省略则使用宿主时限"
        if not isinstance(env, dict) or len(env) > 32 or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\x00" in value or len(value) > 4096
                for key, value in env.items()):
            return "错误：env 必须是至多 32 项的字符串环境变量"
        self.jobs.poll()
        running = [r for r in self.jobs.records.values() if r["status"] == "running"]
        if running and (access == "write" or any(r.get("access", "write") == "write" for r in running)):
            return "错误：写命令与其他项目命令互斥；先等待或取消。多个 read_only 后台命令可以并发。"
        try:
            directory = self.workspace.directory(cwd)
        except ValueError as exc:
            return "错误：" + str(exc)
        inside_cwd = "/work" + ("/" + directory.relative_to(self.workspace.root).as_posix()
                                if directory != self.workspace.root else "")
        sandbox = ["bwrap", "--die-with-parent", "--unshare-pid"]
        if self.network_boundary == "bwrap network namespace":
            sandbox.append("--unshare-net")
        for folder in ("/usr", "/bin", "/lib", "/lib64"):
            if os.path.exists(folder):
                sandbox += ["--ro-bind", folder, folder]
        sandbox += ["--bind", str(self.jobs.scratch), "/tmp", "--proc", "/proc", "--dev", "/dev",
                    "--ro-bind" if access == "read_only" else "--bind", str(self.workspace.root), "/work"]
        # Support libraries are immutable even for commands allowed to edit source.
        library = self.workspace.root / ".venv"
        if library.is_dir():
            sandbox += ["--ro-bind", str(library), "/work/.venv"]
        sandbox += ["--chdir", inside_cwd, "--", *argv]
        clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
                     "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", **env}
        revision = self.workspace.refresh()
        try:
            job_id = self.jobs.start(sandbox, label=label, cwd=self.workspace.root, env=clean_env,
                                     timeout=timeout_s,
                                     metadata={"argv": argv, "cwd": cwd, "env_names": sorted(env), "access": access,
                                               "workspace_revision_start": revision,
                                               "execution_environment": f"bwrap:/work {access}; .venv ro; /tmp task-local shared; " + self.network_boundary})
        except RunTimedOut:
            raise
        except RuntimeError as exc:
            if self.jobs.events is not None and self.jobs.events.observer_error is not None:
                raise
            return "错误：" + str(exc)
        if mode == "background":
            self.jobs.emit("job.backgrounded", job_id=job_id, reason="requested")
            return self.jobs.describe(job_id)
        return self.jobs.wait(job_id)

    def run_command(self, argv, cwd, env, mode, access="write", timeout_s=None):
        return self._command(argv, cwd, env, mode, "command", access, timeout_s)

    def run_check(self, mode, access="write", timeout_s=None):
        if not self.check_argv:
            return "错误：未配置预设检查命令，请用 run_command 执行针对性验证"
        return self._command(self.check_argv, ".", {}, mode, "project_check", access, timeout_s)

    def tools(self, *, reviewer=False):
        w = self.workspace
        read = [
            Tool("list_files", "列出工作区根目录或指定子目录；跳过缓存和符号链接。",
                 {"directory": Parameter("相对目录，根目录用 .")}, w.list_files, concurrency="shared"),
            Tool("search_text", "在工作区 UTF-8 文件中按字面文本搜索，返回文件和行号。",
                 {"query": Parameter("字面搜索文本"), "max_results": Parameter("1 到 100", "integer")},
                 w.search_text, concurrency="shared"),
            Tool("read_file", "读取工作区 UTF-8 文件的闭区间行号，返回行号与 SHA256 摘要。",
                 {"name": Parameter("相对路径"), "start_line": Parameter("起始行，从 1 开始", "integer"),
                  "end_line": Parameter("结束行，包含该行", "integer")}, w.read_file, concurrency="shared"),
            Tool("get_diff", "查看本次任务开始以来的文本差异及工作区修订号。", {}, w.get_diff, concurrency="shared"),
        ]
        if reviewer:
            return ToolRegistry(read)
        extra = ([Tool("run_check", "执行本项目预设的验证命令，记录对应工作区修订。",
                       {"mode": Parameter("foreground 或 background"),
                        "access": Parameter("read_only 可与其他只读命令并发；默认 write", required=False),
                        "timeout_s": Parameter("可选单命令时限，正整数秒；不会超过剩余任务预算", "integer", required=False)}, self.run_check, interrupt="background")]
                 if self.check_argv else [])
        return ToolRegistry([*read,
            Tool("edit_file", "唯一匹配替换，须提供 read_file 返回的旧 SHA256；文件变化时拒绝覆盖。",
                 {"name": Parameter("相对路径"), "expected_hash": Parameter("读取时的 SHA256"),
                  "old_text": Parameter("唯一匹配的原文"), "new_text": Parameter("替换后的原文")}, self.edit_file),
            Tool("create_file", "创建新 UTF-8 文件；已有文件绝不覆盖。",
                 {"name": Parameter("相对路径"), "content": Parameter("完整文件内容")}, self.create_file),
            Tool("run_command", "执行 argv，不经过 shell。默认 write 独占；read_only 强制 /work 只读，可并发启动多个后台命令。/tmp 在本任务的命令间共享。宿主无固定 120 秒上限；实际时限见返回 timeout_s。",
                 {"argv": Parameter("命令及参数", "string_array"), "cwd": Parameter("相对工作目录，根目录用 ."),
                  "env": Parameter("附加环境变量，通常填 {}", "string_map"),
                  "mode": Parameter("foreground 或 background"),
                  "access": Parameter("可选 read_only 或 write；默认 write。只读测试若需写编译产物请使用 write", required=False),
                  "timeout_s": Parameter("可选单命令时限，正整数秒；省略使用宿主预算", "integer", required=False)}, self.run_command, interrupt="background"),
            *extra,
        ])
