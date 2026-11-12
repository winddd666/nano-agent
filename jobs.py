"""本次运行拥有的子进程任务；事件与历史只由主循环线程写入。"""

import codecs
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

from tool_registry import Parameter, Tool
from context import DEFAULT_TOOL_OUTPUT_CHARS, summarize_text
from control import RunTimedOut


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_SIGUSR2_BIT = 1 << (signal.SIGUSR2 - 1)
_STACK_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+),? in ([^\n]+)', re.M)
_SITECUSTOMIZE_SRC = (
    "def _nano_install():\n"
    "    import faulthandler, functools, importlib.machinery, importlib.util, os, signal, sys\n"
    "    _self_mod = sys.modules.get('sitecustomize')\n"
    "    _hook_entry = next((p for p in list(sys.path) if '/nano_hook/' in p or ('/nano-jobs-' in p and '/hook/' in p)), None)\n"
    "    if _hook_entry is not None:\n"
    "        sys.path[:] = [p for p in sys.path if p != _hook_entry]\n"
    "    if _hook_entry is not None:\n"
    "        try:\n"
    "            _clean_hook = _hook_entry.rstrip('/')\n"
    "            _jid = os.path.basename(_clean_hook)\n"
    "            _ddir = '/run/nano_diag' if os.path.isdir('/run/nano_diag') else os.path.join(os.path.dirname(os.path.dirname(_clean_hook)), 'diag')\n"
    "            _orig_sig = signal.signal\n"
    "            _orig_reg = faulthandler.register\n"
    "            _orig_unreg = faulthandler.unregister\n"
    "            _st = {'fd': None, 'path': None}\n"
    "            def _bind():\n"
    "                p = os.path.join(_ddir, f'{_jid}.{os.getpid()}.stack')\n"
    "                fd = open(p, 'ab', buffering=0)\n"
    "                _orig_reg(signal.SIGUSR2, file=fd, all_threads=False)\n"
    "                _st['fd'], _st['path'] = fd, p\n"
    "            def _drop_fd():\n"
    "                fd, p = _st['fd'], _st['path']\n"
    "                if fd is None:\n"
    "                    return\n"
    "                _st['fd'], _st['path'] = None, None\n"
    "                for fn in (fd.close, lambda: os.unlink(p)):\n"
    "                    try:\n"
    "                        fn()\n"
    "                    except Exception:\n"
    "                        pass\n"
    "            def _is_usr2(sig):\n"
    "                try:\n"
    "                    return int(sig) == int(signal.SIGUSR2)\n"
    "                except Exception:\n"
    "                    return False\n"
    "            @functools.wraps(_orig_sig)\n"
    "            def _sig_wrap(signalnum, handler):\n"
    "                res = _orig_sig(signalnum, handler)\n"
    "                if _is_usr2(signalnum):\n"
    "                    _drop_fd()\n"
    "                return res\n"
    "            @functools.wraps(_orig_reg)\n"
    "            def _reg_wrap(*a, **kw):\n"
    "                res = _orig_reg(*a, **kw)\n"
    "                if _is_usr2(a[0] if a else kw.get('signum')):\n"
    "                    _drop_fd()\n"
    "                return res\n"
    "            @functools.wraps(_orig_unreg)\n"
    "            def _unreg_wrap(*a, **kw):\n"
    "                if len(a) == 1 and not kw and _is_usr2(a[0]) and _st['fd'] is not None:\n"
    "                    try:\n"
    "                        _orig_unreg(signal.SIGUSR2)\n"
    "                    except Exception:\n"
    "                        pass\n"
    "                    _drop_fd()\n"
    "                return _orig_unreg(*a, **kw)\n"
    "            def _after_fork():\n"
    "                fd = _st['fd']\n"
    "                if fd is None:\n"
    "                    return\n"
    "                _st['fd'], _st['path'] = None, None\n"
    "                try:\n"
    "                    fd.close()\n"
    "                except Exception:\n"
    "                    pass\n"
    "                try:\n"
    "                    _bind()\n"
    "                except Exception:\n"
    "                    pass\n"
    "            _bind()\n"
    "            signal.signal = _sig_wrap\n"
    "            faulthandler.register = _reg_wrap\n"
    "            faulthandler.unregister = _unreg_wrap\n"
    "            if hasattr(os, 'register_at_fork'):\n"
    "                os.register_at_fork(after_in_child=_after_fork)\n"
    "        except Exception:\n"
    "            pass\n"
    "    _spec = importlib.machinery.PathFinder.find_spec('sitecustomize', sys.path)\n"
    "    if _spec is not None and _spec.loader is not None:\n"
    "        _mod = importlib.util.module_from_spec(_spec)\n"
    "        sys.modules['sitecustomize'] = _mod\n"
    "        _spec.loader.exec_module(_mod)\n"
    "    elif _self_mod is not None:\n"
    "        sys.modules['sitecustomize'] = _self_mod\n"
    "_nano_install()\n"
    "if '_nano_install' in globals():\n"
    "    del _nano_install\n"
)


def _diagnose_command_hint(record, text: str) -> str:
    """对高频沙箱环境错配给出单行非侵入式操作提示。"""
    if record.get("status") != "failed" or record.get("exit_code") in (0, None) or record.get("reason") == "timeout":
        return ""
    if "No module named pytest" in text or "No module named 'pytest'" in text:
        return "当前环境未安装 pytest；请改用 python3 -m unittest 或 python3 -c 直接导入并调用目标测试函数。"
    if "No module named 'sympy.testing'" in text:
        return "当前版本 SymPy 无 sympy.testing；请直接导入目标 tests/test_*.py 调用测试函数，或使用 sympy.utilities.runtests。"
    if "not a git repository" in text.lower():
        return "当前工作区不含 .git 仓库；请调用内置 get_diff 工具查看工作区变更与修订号。"
    if ("Read-only file system" in text or "EROFS" in text) and record.get("access") == "read_only":
        return "当前命令为 access='read_only'（/work 只读）；临时文件请写到 /tmp，确需修改 /work 请传 access='write'。"
    return ""


def _proc_sandbox_tree(root_pid):
    """返回 (根进程名, 沙箱内所有非 bwrap/timeout 子进程指标列表)。"""
    children_map, info_map = {}, {}
    proc = Path("/proc")
    if not proc.is_dir():
        return None, []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            rparen = stat.rfind(")")
            if rparen == -1:
                continue
            comm = stat[stat.find("(") + 1 : rparen]
            parts = stat[rparen + 2 :].split()
            state = parts[0]
            ppid = int(parts[1])
            self_cpu = (int(parts[11]) + int(parts[12])) / _CLK_TCK
            child_cpu = (int(parts[13]) + int(parts[14])) / _CLK_TCK
            children_map.setdefault(ppid, []).append(pid)
            info_map[pid] = {
                "pid": pid, "ppid": ppid, "comm": comm, "state": state,
                "self_cpu_s": self_cpu, "child_cpu_s": child_cpu,
            }
        except (OSError, ValueError, IndexError):
            continue
    root_info = info_map.get(root_pid)
    root_comm = root_info["comm"] if root_info else None
    inner, stack = [], []
    if root_info and root_comm not in ("bwrap", "timeout"):
        inner.append(root_info)
    stack.extend(children_map.get(root_pid, []))
    while stack:
        cur = stack.pop()
        cur_info = info_map.get(cur)
        if cur_info and cur_info["comm"] not in ("bwrap", "timeout"):
            inner.append(cur_info)
        stack.extend(children_map.get(cur, []))
    return root_comm, inner


def _verified_diag_stack(pid, job_id, diag_dir: Path):
    """确认目标进程捕获 SIGUSR2 且当前仍持有 Nano 注入的专属诊断栈文件描述符。"""
    catches_usr2 = False
    ns_pid = pid
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("SigCgt:"):
                catches_usr2 = bool(int(line.split()[1], 16) & _SIGUSR2_BIT)
            elif line.startswith("NSpid:"):
                parts = line.split()[1:]
                if parts:
                    ns_pid = int(parts[-1])
    except (OSError, ValueError, IndexError):
        return None
    if not catches_usr2:
        return None
    stack_file = diag_dir / f"{job_id}.{ns_pid}.stack"
    try:
        hst = stack_file.stat()
    except OSError:
        return None
    if not stat.S_ISREG(hst.st_mode):
        return None
    target_id = (hst.st_dev, hst.st_ino)
    try:
        for fd_entry in Path(f"/proc/{pid}/fd").iterdir():
            try:
                fst = os.stat(fd_entry)
            except OSError:
                continue
            if (fst.st_dev, fst.st_ino) == target_id:
                return stack_file
    except OSError:
        return None
    return None


def _format_live_stack(raw_dump: str) -> str:
    """将 faulthandler 栈（内层在前）翻转为外->内精简调用链。"""
    frames = []
    for fpath, lineno, func in reversed(_STACK_FRAME_RE.findall(raw_dump)):
        clean = fpath[6:] if fpath.startswith("/work/") else fpath.rsplit("/", 1)[-1]
        item = f"{clean}:{lineno}:{func.strip()}"
        if not frames or frames[-1] != item:
            frames.append(item)
    if not frames:
        return ""
    if len(frames) > 6:
        frames = frames[:2] + ["..."] + frames[-4:]
    return " -> ".join(frames)


class JobManager:
    def __init__(self, *, cwd=None, checks_command=None, timeout=None, output_limit=1024 * 1024,
                 max_jobs=4, run_timeout=None, max_timeout=None, foreground_yield_s=None,
                 wait_timeout_s=None, max_wait_timeout_s=60, stop_with_sigint=False,
                 sigint_grace_s=1.5, smart_excerpt=False, live_diag=None):
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
            ("sigint_grace_s", sigint_grace_s),
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
        self.stop_with_sigint = bool(stop_with_sigint)
        self.sigint_grace_s = sigint_grace_s
        self.smart_excerpt = bool(smart_excerpt)
        self.live_diag = bool(stop_with_sigint) if live_diag is None else bool(live_diag)
        self.deadline = monotonic() + run_timeout if run_timeout is not None else None
        self.directory = tempfile.TemporaryDirectory(prefix="nano-jobs-")
        self.scratch = Path(self.directory.name) / "scratch"
        self.scratch.mkdir()
        self.hook_dir = Path(self.directory.name) / "hook"
        self.diag_dir = Path(self.directory.name) / "diag"
        if self.live_diag:
            self.hook_dir.mkdir()
            self.diag_dir.mkdir()
            (self.hook_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE_SRC, encoding="utf-8")
        self.records, self._processes = {}, {}
        self.events = None
        self.inbox = None
        self.origin = {"task_version": 0, "call_id": None}
        self.closed = False
        self.on_wait = None

    def emit(self, event_type, **data):
        if self.events is not None:
            self.events.emit(event_type, **data)

    def peek_next_id(self):
        return f"job-{len(self.records) + 1}"

    def start(self, argv, *, label, cwd=None, env=None, metadata=None, timeout=None, diag_mount=None):
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
        job_id = self.peek_next_id()
        path = Path(self.directory.name) / (job_id + ".log")
        spawn_env = env
        if self.live_diag:
            spawn_env = dict(os.environ if env is None else env)
            if diag_mount:
                hook_target = f"/run/nano_hook/{job_id}"
            else:
                host_job_hook = self.hook_dir / job_id
                if not host_job_hook.is_dir():
                    host_job_hook.mkdir(parents=True, exist_ok=True)
                    (host_job_hook / "sitecustomize.py").write_text(_SITECUSTOMIZE_SRC, encoding="utf-8")
                hook_target = str(host_job_hook)
            existing_py = spawn_env.get("PYTHONPATH", "")
            spawn_env["PYTHONPATH"] = f"{hook_target}:{existing_py}" if existing_py else hook_target
        self.emit("job.starting", job_id=job_id, label=label, **self.origin)
        try:
            with path.open("wb") as output:
                process = subprocess.Popen(argv, cwd=cwd or self.cwd, env=spawn_env, stdin=subprocess.DEVNULL,
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
        if record["status"] == "failed" and reason != "timeout":
            hint = _diagnose_command_hint(record, self._load_text(record))
            if hint:
                record["hint"] = hint
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
            if self.stop_with_sigint:
                root_comm, inner = _proc_sandbox_tree(process.pid)
                sent_int = False
                if root_comm == "bwrap":
                    for item in inner:
                        try:
                            os.kill(item["pid"], signal.SIGINT)
                            sent_int = True
                        except (ProcessLookupError, PermissionError):
                            pass
                else:
                    try:
                        os.killpg(process.pid, signal.SIGINT)
                        sent_int = True
                    except (ProcessLookupError, PermissionError):
                        pass
                if sent_int:
                    try:
                        process.wait(timeout=self.sigint_grace_s)
                    except subprocess.TimeoutExpired:
                        pass
            if process.poll() is None:
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
            raw = output_file.read(self.output_limit)
        final = record.get("status") != "running" or len(raw) >= self.output_limit
        return codecs.getincrementaldecoder("utf-8")("replace").decode(raw, final=final)

    def _sample_live_diag(self, job_id, total_chars=None):
        record = self.records[job_id]
        process = self._processes.get(job_id)
        if not self.live_diag or record["status"] != "running" or process is None:
            return None
        if total_chars is None:
            total_chars = len(self._load_text(record))
        now = monotonic()
        _, inner = _proc_sandbox_tree(process.pid)
        inner_pids = {e["pid"] for e in inner}
        cpu_s = round(
            sum(e["self_cpu_s"] for e in inner)
            + sum(e["child_cpu_s"] for e in inner if e["ppid"] not in inner_pids),
            3,
        )
        prev = record.get("_last_diag")
        prev_wall = prev["wall"] if prev else record.get("_started", now)
        prev_cpu = prev["cpu_s"] if prev else 0.0
        prev_chars = prev["chars"] if prev else 0
        dt = max(0.001, now - prev_wall)
        cpu_pct = round(min(400.0, max(0.0, cpu_s - prev_cpu) / dt * 100.0), 1)

        current_stack = ""
        if self.diag_dir.is_dir() and inner:
            sig_ready = []
            for e in inner:
                sf = _verified_diag_stack(e["pid"], job_id, self.diag_dir)
                if sf is not None:
                    sig_ready.append((e, sf))
            if sig_ready:
                ready_pids = {e["pid"] for e, _ in sig_ready}
                inner_children = {}
                for e in inner:
                    inner_children.setdefault(e["ppid"], []).append(e["pid"])

                def _has_ready_descendant(pid):
                    stack_q = list(inner_children.get(pid, []))
                    while stack_q:
                        cur = stack_q.pop()
                        if cur in ready_pids:
                            return True
                        stack_q.extend(inner_children.get(cur, []))
                    return False

                leaves = [pair for pair in sig_ready if not _has_ready_descendant(pair[0]["pid"])] or sig_ready
                target, target_file = max(
                    leaves,
                    key=lambda pair: (pair[0]["state"] == "R", pair[0]["self_cpu_s"], pair[0]["pid"]),
                )
                try:
                    before_sz = target_file.stat().st_size
                except OSError:
                    before_sz = None
                signaled = False
                if before_sz is not None:
                    try:
                        os.kill(target["pid"], signal.SIGUSR2)
                        signaled = True
                    except (ProcessLookupError, PermissionError):
                        pass
                if signaled:
                    grown = False
                    deadline = monotonic() + 0.15
                    while monotonic() < deadline:
                        try:
                            if target_file.stat().st_size > before_sz:
                                grown = True
                                break
                        except OSError:
                            break
                        sleep(0.01)
                    if grown:
                        try:
                            with target_file.open("rb") as sf:
                                sf.seek(before_sz)
                                raw_dump = sf.read(65536).decode("utf-8", errors="replace")
                            current_stack = _format_live_stack(raw_dump)
                        except OSError:
                            pass

        active_proc = " -> ".join(dict.fromkeys(e["comm"] for e in inner))
        stack_sig = current_stack or active_proc
        if total_chars > prev_chars:
            state = "streaming"
        elif cpu_pct < 2.0 and not any(e["state"] == "R" for e in inner):
            state = "idle_blocked"
        else:
            state = "cpu_busy"

        diag = {"state": state, "cpu_s": cpu_s, "cpu_pct": cpu_pct}
        if current_stack:
            diag["current_stack"] = current_stack
        elif active_proc:
            diag["active_proc"] = active_proc
        if prev is not None and stack_sig and prev.get("stack_sig"):
            diag["stack_moved"] = stack_sig != prev["stack_sig"]

        record["_last_diag"] = {
            "wall": now, "cpu_s": cpu_s, "chars": total_chars, "stack_sig": stack_sig,
        }
        return diag

    def describe(self, job_id, *, include_diag=True):
        self.poll()
        if job_id not in self.records:
            return "错误：本次运行中没有这个任务。"
        payload = self.snapshot(job_id)
        record = self.records[job_id]
        if record["status"] == "running" and "_started" in record:
            payload["elapsed_s"] = round(monotonic() - record["_started"], 3)
        if include_diag and self.live_diag and record["status"] == "running":
            diag = self._sample_live_diag(job_id)
            if diag is not None:
                payload["live_diag"] = diag
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
            if self.live_diag:
                diag = self._sample_live_diag(job_id, total_chars=total)
                if diag is not None:
                    base["live_diag"] = diag
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
            hint = _diagnose_command_hint(record, text)
            if hint:
                payload["hint"] = hint
            return json.dumps(payload, ensure_ascii=False)
        record["_wait_offset"] = total
        payload = {**base, "output": text, "next_offset": total, "output_chars": total}
        if prev_offset > 0:
            payload["output_offset"] = 0
            payload["delta_chars"] = total - prev_offset
        if wait_clamped:
            payload["wait_clamped"] = True
        hint = _diagnose_command_hint(record, text)
        if hint:
            payload["hint"] = hint
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
            tail_note = (
                "到期若仍在运行不会取消任务，并附带进程活性与实时调用栈进展快照。"
                if self.live_diag
                else "到期若仍在运行不会取消任务，返回增量输出与 running 状态。"
            )
            wait_desc = f"有界等待任务（默认 {int(self.wait_timeout_s)} 秒{cap_note}）并返回新增输出。" + tail_note
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
                    record.pop("_last_diag", None)
        finally:
            self.directory.cleanup()
        if error is not None:
            raise error
