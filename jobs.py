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
from context import DEFAULT_TOOL_OUTPUT_CHARS, summarize_text
from control import RunTimedOut


class JobManager:
    def __init__(self, *, cwd=None, checks_command=None, timeout=None, output_limit=1024 * 1024,
                 max_jobs=4, run_timeout=None, max_timeout=None, foreground_yield_s=None,
                 wait_timeout_s=None, max_wait_timeout_s=60, smart_excerpt=False):
        self.cwd = Path(cwd or Path(__file__).resolve().parent)
        # 命令由宿主配置，模型仅选择等待方式，不接收任意 shell/code 参数。
        self.checks_command = checks_command or [sys.executable, "-m", "unittest", "-q"]
        self.timeout, self.output_limit = timeout, output_limit
        for name, val in (
            ("command timeout", timeout),
            ("run_timeout", run_timeout),
            ("max_timeout", max_timeout),
            ("foreground_yield_s", foreground_yield_s),
            ("wait_timeout_s", wait_timeout_s),
            ("max_wait_timeout_s", max_wait_timeout_s),
        ):
            if val is not None and val <= 0:
                raise ValueError(f"{name} must be positive or None")
        if type(max_jobs) is not int or max_jobs < 1:
            raise ValueError("max_jobs must be a positive integer")
        self.max_jobs = max_jobs
        self.max_timeout = max_timeout
        self.foreground_yield_s = foreground_yield_s
        self.wait_timeout_s = wait_timeout_s
        self.max_wait_timeout_s = max_wait_timeout_s
        self.smart_excerpt = bool(smart_excerpt)
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
        clamped = False
        if limit is None and self.max_timeout is not None:
            limit = self.max_timeout
        elif limit is not None and self.max_timeout is not None and limit > self.max_timeout:
            limit = self.max_timeout
            clamped = True
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
        if clamped:
            record["timeout_clamped"] = True
        self.records[job_id] = record
        record["_path"], record["_started"], record["_wait_offset"] = path, monotonic(), 0
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
            if self.smart_excerpt:
                full_text = self._load_text(record)
                excerpt, truncated = summarize_text(full_text, 2000, smart=True)
                raw_truncated = truncated or record["_path"].stat().st_size > self.output_limit
                self.inbox.notify_job({**self.snapshot(job_id), "output_excerpt": excerpt,
                                       "output_truncated": raw_truncated})
            else:
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

    def _load_text(self, record):
        if "_path" not in record:
            return record.get("output", "")
        with record["_path"].open("rb") as output_file:
            return output_file.read(self.output_limit).decode("utf-8", errors="replace")

    def describe(self, job_id, *, include_diag=True):
        self.poll()
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        payload = self.snapshot(job_id)
        record = self.records[job_id]
        if record["status"] == "running" and "_started" in record:
            payload["elapsed_s"] = round(monotonic() - record["_started"], 3)
        return json.dumps(payload, ensure_ascii=False)

    def read(self, job_id, offset, *, full=False):
        if type(offset) is not int or offset < 0:
            return "错误：offset 必须是非负整数。"
        self.poll()
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        record = self.records[job_id]
        text = self._load_text(record)
        output = text[offset:] if full else text[offset:offset + 3200]
        return json.dumps({**self.snapshot(job_id), "output": output,
                           "next_offset": offset + len(output), "output_chars": len(text)}, ensure_ascii=False)

    def _progress_snapshot(self, job_id, *, yielded=False, yield_reason=None, wait_clamped=False):
        record = self.records[job_id]
        text = self._load_text(record)
        total = len(text)
        prev_offset = record.get("_wait_offset", 0)
        base = self.snapshot(job_id)
        if record["status"] == "running" and "_started" in record:
            base["elapsed_s"] = round(monotonic() - record["_started"], 3)
        if yielded:
            chunk = text[prev_offset:]
            record["_wait_offset"] = total
            payload = {
                **base,
                "yielded": True,
                "yield_reason": yield_reason,
                "output": chunk,
                "output_offset": prev_offset,
                "delta_chars": total - prev_offset,
                "next_offset": total,
                "output_chars": total,
            }
            if wait_clamped:
                payload["wait_clamped"] = True
            return json.dumps(payload, ensure_ascii=False)
        if prev_offset > 0 and total > DEFAULT_TOOL_OUTPUT_CHARS:
            chunk = text[prev_offset:]
            record["_wait_offset"] = total
            payload = {
                **base,
                "output": chunk,
                "output_offset": prev_offset,
                "delta_chars": total - prev_offset,
                "next_offset": total,
                "output_chars": total,
            }
            if wait_clamped:
                payload["wait_clamped"] = True
            return json.dumps(payload, ensure_ascii=False)
        record["_wait_offset"] = total
        payload = {**base, "output": text, "next_offset": total, "output_chars": total}
        if prev_offset > 0:
            payload["output_offset"] = 0
            payload["delta_chars"] = total - prev_offset
        if wait_clamped:
            payload["wait_clamped"] = True
        return json.dumps(payload, ensure_ascii=False)

    def background_requested(self, job_id=None):
        controls = self.inbox.take_background() if self.inbox is not None else []
        for item in controls:
            self.emit("control.handled", **item, job_id=job_id,
                      outcome="backgrounded" if job_id else "no_foreground_job")
        return bool(controls)

    def wait(self, job_id, wait_s=None, *, _max_wait_s=None, _yield_reason="wait_timeout"):
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        if wait_s is not None and (type(wait_s) is not int or wait_s <= 0):
            return "错误：wait_s 必须为正整数秒数。"
        wait_clamped = False
        if _max_wait_s is not None:
            budget_s = _max_wait_s
        else:
            budget_s = self.wait_timeout_s if wait_s is None else wait_s
            if budget_s is not None and self.max_wait_timeout_s is not None and budget_s > self.max_wait_timeout_s:
                budget_s = self.max_wait_timeout_s
                wait_clamped = True
        self.emit("job.waiting", job_id=job_id)
        wait_started = monotonic()
        while True:
            if self.on_wait is not None:
                self.on_wait()
            self.poll()
            if self.records[job_id]["status"] != "running":
                return self._progress_snapshot(job_id, wait_clamped=wait_clamped)
            urgent = self.inbox is not None and self.inbox.urgent()
            if self.background_requested(job_id) or urgent:
                reason = "send_now" if urgent else "background"
                self.emit("job.backgrounded", job_id=job_id, reason=reason)
                return self._progress_snapshot(job_id, yielded=True, yield_reason=reason, wait_clamped=wait_clamped)
            if budget_s is not None and monotonic() - wait_started >= budget_s:
                self.emit("job.backgrounded", job_id=job_id, reason=_yield_reason)
                return self._progress_snapshot(job_id, yielded=True, yield_reason=_yield_reason, wait_clamped=wait_clamped)
            sleep(0.05)

    def wait_foreground(self, job_id):
        return self.wait(job_id, _max_wait_s=self.foreground_yield_s, _yield_reason="auto_yield")

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
            return self.wait_foreground(job_id)
        self.emit("job.backgrounded", job_id=job_id, reason="requested")
        return self.describe(job_id, include_diag=False)

    def cancel(self, job_id):
        self.poll()
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        self._stop(job_id, "requested")
        return self._progress_snapshot(job_id)

    def tools(self):
        key = {"job_id": Parameter("本次运行中启动的任务编号，例如 job-1。")}
        wait_params = dict(key)
        wait_desc = "等待任务结束并取得输出；等待不消耗模型请求，可由用户立即处理或后台化指令让出前台。"
        if self.wait_timeout_s is not None:
            cap_note = f"，上限 {self.max_wait_timeout_s} 秒" if self.max_wait_timeout_s is not None else ""
            wait_params["wait_s"] = Parameter(
                f"可选本次最多等待秒数，默认 {int(self.wait_timeout_s)} 秒{cap_note}；到期未结束不杀进程，返回增量输出与 running 状态。",
                "integer",
                required=False,
            )
            wait_desc = f"有界等待任务（默认 {int(self.wait_timeout_s)} 秒{cap_note}）并返回新增输出。到期若仍在运行不会取消任务，返回增量输出与 running 状态。"
        return [
            Tool("run_project_checks", "运行本项目 unittest 检查。foreground 等待，background 返回任务编号；running 不表示成功。",
                 {"mode": Parameter("foreground 或 background。")}, self.run_checks, interrupt="background"),
            Tool("job_status", "查看任务状态及退出码，不重新执行任务。", key, self.describe, concurrency="control"),
            Tool("read_job_output", "读取任务当前输出；offset 为从零开始的字符偏移，至多返回 3200 字符。",
                 {**key, "offset": Parameter("非负字符偏移。", "integer")}, self.read, concurrency="control"),
            Tool("wait_job", wait_desc, wait_params, self.wait, interrupt="background"),
            Tool("cancel_job", "停止本次运行拥有的任务进程组，确认退出后返回状态与当前输出；已经发生的效果不会回滚。", key, self.cancel, concurrency="control"),
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
                    record.pop("_wait_offset", None)
        finally:
            self.directory.cleanup()
        if error is not None:
            raise error
