"""并发接收消息，主循环在安全节点取走；输入线程不修改历史或写事件。"""

import codecs
import os
import select
import sys
import termios
from datetime import datetime, timezone
from copy import deepcopy
from threading import Event, Lock, Thread


class InputInbox:
    def __init__(self):
        self._lock = Lock()
        self._pending = []
        self._next_id = 1
        self._closed = False
        self._error = None
        self._notified = set()

    def _put(self, kind, priority, **data):
        item = {"id": self._next_id, "kind": kind, "priority": priority,
                "received_at": datetime.now(timezone.utc).isoformat(), **data}
        self._next_id += 1
        self._pending.append(item)
        return item["id"]

    def _take(self, predicate):
        chosen = [item for item in self._pending if predicate(item)]
        self._pending = [item for item in self._pending if not predicate(item)]
        priority = {"now": 0, "next": 1, "later": 2}
        return sorted(chosen, key=lambda item: (priority[item["priority"]], item["id"]))

    def submit(self, text, *, immediate=False):
        """返回已接收消息的 id；关闭后明确拒收，避免静默丢失。"""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("追加消息必须是非空字符串")
        with self._lock:
            if self._closed:
                raise RuntimeError("本次运行已关闭输入，请启动新任务。")
            return self._put("user", "now" if immediate else "next", text=text,
                             mode="immediate" if immediate else "normal")

    def send_now(self, text="请立即基于已有证据回答当前任务，说明未完成的工作，不要把运行中任务说成成功。"):
        return self.submit(text, immediate=True)

    def background(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("本次运行已关闭输入，请启动新任务。")
            self._put("control", "now", action="background")

    def notify_job(self, snapshot):
        """终态通知去重；关闭后不再引入工作。返回入队 id 或 None。"""
        if snapshot.get("status") not in ("succeeded", "failed", "cancelled"):
            raise ValueError("只允许终态任务通知")
        with self._lock:
            key = snapshot["job_id"]
            if self._closed or key in self._notified:
                return None
            self._notified.add(key)
            return self._put("notification", "later", payload=deepcopy(snapshot))

    def acknowledge_job(self, job_id):
        """终态已随本批工具结果交付，避免再向模型重复注入通知。"""
        with self._lock:
            return self._take(lambda item: item["kind"] == "notification" and item["payload"]["job_id"] == job_id)

    def urgent(self):
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            return any(item["kind"] == "user" and item["priority"] == "now" for item in self._pending)

    def take_background(self):
        with self._lock:
            return self._take(lambda item: item["kind"] == "control")

    def fail(self, error):
        with self._lock:
            if not self._closed:
                self._error = error

    def drain(self):
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            return sorted(self._take(lambda item: item["kind"] == "user"), key=lambda item: item["id"])

    def drain_events(self):
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            return self._take(lambda item: True)

    def seal_if_empty(self):
        """与 submit 共用锁：交付关门时，之前已接收的消息必须被处理。"""
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            if self._pending:
                return False
            self._closed = True
            return True

    def close(self):
        with self._lock:
            self._closed = True
            return sorted(self._take(lambda item: item["kind"] == "user"), key=lambda item: item["id"])

    def close_events(self):
        with self._lock:
            self._closed = True
            return self._take(lambda item: True)


class StdinInput:
    """显式启用的按行输入；EOF 仅结束接收，退出时不遗留阻塞读取线程。"""
    def __init__(self, inbox, stream=None):
        self.inbox = inbox
        self.stream = sys.stdin if stream is None else stream
        self.stop = Event()

    def __enter__(self):
        self.fd = self.stream.fileno()
        self.terminal = None
        if self.stream.isatty():
            self.terminal = termios.tcgetattr(self.fd)
            settings = termios.tcgetattr(self.fd)
            # 保留信号与终端回显；按字符接收，Ctrl+B 不必等到回车才到达程序。
            settings[3] &= ~termios.ICANON
            settings[6][termios.VMIN] = 1
            settings[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, settings)
        self.thread = Thread(target=self._read, name="agent-input", daemon=True)
        self.thread.start()
        return self

    def _read(self):
        decoder = codecs.getincrementaldecoder(self.stream.encoding or "utf-8")(errors="replace")
        buffered = ""
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([self.fd], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(self.fd, 4096)
                for char in decoder.decode(chunk, final=not chunk):
                    if char == "\x02":
                        self.inbox.background()
                    elif char == "\x04":
                        if buffered.strip():
                            self._submit(buffered)
                        return
                    elif char in ("\x7f", "\b"):
                        buffered = buffered[:-1]
                    elif char == "\n":
                        if buffered.strip():
                            self._submit(buffered.rstrip("\r"))
                        buffered = ""
                    else:
                        buffered += char
                if not chunk:
                    if buffered.strip():
                        self._submit(buffered)
                    return
        except OSError as exc:
            self.inbox.fail(f"运行中输入读取失败：{type(exc).__name__}")
        except RuntimeError:
            # 主循环已经关闭输入，结束接收，不再向已完成任务追加。
            return

    def _submit(self, line):
        if line == "/bg":
            self.inbox.background()
        elif line == "/now":
            self.inbox.send_now()
        elif line.startswith("/now "):
            self.inbox.send_now(line[5:])
        else:
            self.inbox.submit(line)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=1)
        if self.terminal is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.terminal)
