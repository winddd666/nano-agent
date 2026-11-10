"""本次运行拥有的子进程任务；事件与历史只由主循环线程写入。"""

import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

from tool_registry import Parameter, Tool
from control import RunTimedOut


class JobManager:
    def __init__(self, *, cwd=None, checks_command=None, timeout=None, output_limit=1024 * 1024,
                 max_jobs=4, run_timeout=None):
        self.cwd = Path(cwd or Path(__file__).resolve().parent)
        # 命令由宿主配置，模型仅选择等待方式，不接收任意 shell/code 参数。
        self.checks_command = checks_command or [sys.executable, "-m", "unittest", "-q"]
        self.timeout, self.output_limit = timeout, output_limit
        if timeout is not None and timeout <= 0:
            raise ValueError("command timeout must be positive or None")
        if type(max_jobs) is not int or max_jobs < 1:
            raise ValueError("max_jobs must be a positive integer")
        if run_timeout is not None and run_timeout <= 0:
            raise ValueError("run_timeout must be positive or None")
        self.max_jobs = max_jobs
        self.deadline = monotonic() + run_timeout if run_timeout is not None else None
        self.directory = tempfile.TemporaryDirectory(prefix="nano-jobs-")
        self.scratch = Path(self.directory.name) / "scratch"
        self.scratch.mkdir()
        self.records, self._processes = {}, {}
        self.events = None
        self.inbox = None
        self.origin = {"task_version": 0, "call_id": None}
        self.closed = False
        self.on_wait = None

    def emit(self, event_type, **data):
        if self.events is not None:
            self.events.emit(event_type, **data)

    def start(self, argv, *, label, cwd=None, env=None, metadata=None, timeout=None):
        if self.closed:
            raise RuntimeError("后台任务管理器已关闭。")
        self.poll()
        if sum(r["status"] == "running" for r in self.records.values()) >= self.max_jobs:
            raise RuntimeError(f"同时运行的任务已达 {self.max_jobs} 个上限。")
        limit = self.timeout if timeout is None else timeout
        if limit is not None and limit <= 0:
            raise ValueError("command timeout must be positive")
        if self.deadline is not None:
            remaining = max(0, self.deadline - monotonic())
            limit = min(limit, remaining) if limit is not None else remaining
        job_id = f"job-{len(self.records) + 1}"
        path = Path(self.directory.name) / (job_id + ".log")
        self.emit("job.starting", job_id=job_id, label=label, **self.origin)
        try:
            with path.open("wb") as output:
                process = subprocess.Popen(argv, cwd=cwd or self.cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise RuntimeError(f"无法启动任务（{type(exc).__name__}）。") from None
        self._processes[job_id] = process
        record = {"job_id": job_id, "label": label, "status": "running", "exit_code": None,
                  "pid": process.pid, "started_at": datetime.now(timezone.utc).isoformat(),
                  **self.origin, **(metadata or {})}
        record["timeout_s"] = limit
        self.records[job_id] = record
        record["_path"], record["_started"] = path, monotonic()
        self.emit("job.started", **self.snapshot(job_id))
        return job_id

    def snapshot(self, job_id):
        record = self.records[job_id]
        return {k: v for k, v in record.items() if not k.startswith("_") and k != "output"}

    def _finish(self, job_id, *, status=None, reason=None):
        record, process = self.records[job_id], self._processes[job_id]
        if record["status"] != "running":
            return
        code = process.poll()
        if code is None:
            return
        # 首次观察到退出时回收同组子进程；不在很久以后的 close 按旧 pid 发信号。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        record.update(status=status or ("succeeded" if code == 0 else "failed"), exit_code=code,
                      finished_at=datetime.now(timezone.utc).isoformat())
        record["elapsed_s"] = round(monotonic() - record["_started"], 6)
        if reason:
            record["reason"] = reason
        if hasattr(self, "completion_hook") and self.completion_hook is not None:
            self.completion_hook(record)
        self.emit("job.completed", **self.snapshot(job_id))
        if self.inbox is not None:
            # 通知正文仅带有界输出；完整输出仍通过任务工具取回。
            with record["_path"].open("rb") as output_file:
                excerpt = output_file.read(2000).decode("utf-8", errors="replace")
            self.inbox.notify_job({**self.snapshot(job_id), "output_excerpt": excerpt,
                                   "output_truncated": record["_path"].stat().st_size > 2000})

    def _stop(self, job_id, reason, *, status="cancelled"):
        record, process = self.records[job_id], self._processes[job_id]
        if record["status"] != "running":
            return
        if process.poll() is not None:
            self._finish(job_id)
            return
        # 先记录取消意图；即使观察器失败也必须清理已拥有的进程。
        try:
            self.emit("job.stopping", job_id=job_id, reason=reason)
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            # 清理仍留在同组的子进程，包括父进程已提前退出的情形。
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            self._finish(job_id, status=status, reason=reason)

    def poll(self):
        if not self.closed and self.deadline is not None and monotonic() >= self.deadline:
            raise RunTimedOut("任务总运行时限已到；正在清理本次任务的进程。")
        for job_id, record in list(self.records.items()):
            if record["status"] != "running":
                continue
            process = self._processes[job_id]
            if process.poll() is not None:
                self._finish(job_id)
            elif record["_path"].stat().st_size > self.output_limit:
                self._stop(job_id, "output_limit", status="failed")
            elif record["timeout_s"] is not None and monotonic() - record["_started"] > record["timeout_s"]:
                self._stop(job_id, "timeout", status="failed")

    def describe(self, job_id):
        self.poll()
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        return json.dumps(self.snapshot(job_id), ensure_ascii=False)

    def read(self, job_id, offset, *, full=False):
        if type(offset) is not int or offset < 0:
            return "错误：offset 必须是非负整数。"
        self.poll()
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        record = self.records[job_id]
        if "_path" not in record:
            text = record["output"]
        else:
            with record["_path"].open("rb") as output_file:
                text = output_file.read(self.output_limit).decode("utf-8", errors="replace")
        output = text[offset:] if full else text[offset:offset + 3200]
        return json.dumps({**self.snapshot(job_id), "output": output,
                           "next_offset": offset + len(output), "output_chars": len(text)}, ensure_ascii=False)

    def background_requested(self, job_id=None):
        controls = self.inbox.take_background() if self.inbox is not None else []
        for item in controls:
            self.emit("control.handled", **item, job_id=job_id,
                      outcome="backgrounded" if job_id else "no_foreground_job")
        return bool(controls)

    def wait(self, job_id):
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        self.emit("job.waiting", job_id=job_id)
        while True:
            if self.on_wait is not None:
                self.on_wait()
            self.poll()
            if self.records[job_id]["status"] != "running":
                return self.read(job_id, 0, full=True)
            urgent = self.inbox is not None and self.inbox.urgent()
            if self.background_requested(job_id) or urgent:
                self.emit("job.backgrounded", job_id=job_id, reason="send_now" if urgent else "background")
                return self.describe(job_id)
            sleep(0.05)

    def run_checks(self, mode):
        if mode not in ("foreground", "background"):
            return "错误：mode 只能是 foreground 或 background。"
        try:
            job_id = self.start(self.checks_command, label="project_checks")
        except RuntimeError as exc:
            # 日志失败是运行错误，不能降级成模型可忽略的普通工具失败。
            if self.events is not None and self.events.observer_error is not None:
                raise
            return "错误：" + str(exc)
        if mode == "foreground":
            return self.wait(job_id)
        self.emit("job.backgrounded", job_id=job_id, reason="requested")
        return self.describe(job_id)

    def cancel(self, job_id):
        self.poll()
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        self._stop(job_id, "requested")
        return self.describe(job_id)

    def tools(self):
        key = {"job_id": Parameter("本次运行中启动的任务编号，例如 job-1。")}
        return [
            Tool("run_project_checks", "运行本项目 unittest 检查。foreground 等待，background 返回任务编号；running 不表示成功。",
                 {"mode": Parameter("foreground 或 background。")}, self.run_checks, interrupt="background"),
            Tool("job_status", "查看任务状态及退出码，不重新执行任务。", key, self.describe, concurrency="control"),
            Tool("read_job_output", "读取任务当前输出；offset 为从零开始的字符偏移，至多返回 3200 字符。",
                 {**key, "offset": Parameter("非负字符偏移。", "integer")}, self.read, concurrency="control"),
            Tool("wait_job", "等待任务结束并取得输出；等待不消耗模型请求，可由用户立即处理或后台化指令让出前台。", key, self.wait, interrupt="background"),
            Tool("cancel_job", "停止本次运行拥有的任务进程组，确认退出后返回状态；已经发生的效果不会回滚。", key, self.cancel, concurrency="control"),
        ]

    def close(self, reason):
        if self.closed:
            return
        self.closed = True
        error = None
        try:
            for job_id in list(self.records):
                try:
                    # 包括已经退出的进程组中可能遗留的普通子进程。
                    if self.records[job_id]["status"] == "running":
                        self._stop(job_id, reason)
                except RuntimeError as exc:
                    error = exc
                finally:
                    record = self.records[job_id]
                    with record["_path"].open("rb") as output_file:
                        record["output"] = output_file.read(self.output_limit).decode("utf-8", errors="replace")
                    record.pop("_path", None)
                    record.pop("_started", None)
        finally:
            self.directory.cleanup()
        if error is not None:
            raise error
