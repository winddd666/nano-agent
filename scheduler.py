"""完整工具批次的有界调度：共享组、独占屏障、控制入口和协作取消。"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Event


@dataclass
class Invocation:
    call: dict
    prepared: object
    result: str | None = None
    cancellation: Event = field(default_factory=Event)
    cancel_sent: bool = False
    deferred: bool = False


class ToolScheduler:
    def __init__(self, registry, events, *, urgent=None, before_start=None, on_result=None,
                 jobs=None, max_workers=4):
        self.registry, self.events = registry, events
        self.urgent = urgent or (lambda: False)
        self.before_start = before_start or (lambda call: None)
        self.on_result = on_result or (lambda call, result: None)
        self.jobs, self.max_workers = jobs, max_workers
        self.nodes = []
        self.waiting = None

    def _finish(self, node, result):
        if not isinstance(result, str):
            raise RuntimeError("工具处理函数必须返回字符串。")
        node.result = result
        try:
            self.events.emit("tool.completed", call_id=node.call["id"], result=result)
        finally:
            self.on_result(node.call, result)

    def _skip(self, node, reason):
        self.events.emit("tool.skipped", call_id=node.call["id"], reason=reason)
        self._finish(node, "错误：本项工具尚未启动，已取消执行。原因：" + reason)

    def _start(self, node):
        if self.urgent():
            self._skip(node, "send_now")
            return False
        self.events.emit("tool.started", call_id=node.call["id"], function=node.call.get("function"))
        if self.urgent():
            self._skip(node, "send_now")
            return False
        self.before_start(node.call)
        return True

    def _invoke(self, node):
        if isinstance(node.prepared, str):
            return node.prepared
        return node.prepared.invoke(node.cancellation)

    def _one(self, node):
        if node.result is not None or not self._start(node):
            return
        try:
            result = self._invoke(node)
        except RuntimeError:
            self._finish(node, "错误：工具执行中止，未取得正常结果。")
            raise
        self._finish(node, result)

    def _pump_controls(self):
        """进程等待时允许后面的已可解析控制动作进入，结果仍按原顺序回填。"""
        if self.waiting is None or self.jobs is None:
            return
        for node in self.nodes[self.waiting + 1:]:
            if node.result is not None or isinstance(node.prepared, str):
                continue
            if node.prepared.tool.concurrency != "control":
                continue
            target = node.prepared.arguments.get("job_id")
            if target is not None and target not in self.jobs.records:
                continue  # 不抢跑尚未创建的任务的控制操作。
            self._one(node)

    def _shared(self, group, pool):
        active, pending = {}, list(group)
        try:
            while pending or active:
                while pending and len(active) < self.max_workers:
                    node = pending.pop(0)
                    if self._start(node):
                        active[pool.submit(self._invoke, node)] = node
                if self.urgent():
                    for node in active.values():
                        policy = node.prepared.tool.interrupt
                        if policy == "cancel" and not node.cancel_sent:
                            node.cancel_sent = True
                            self.events.emit("tool.cancel_requested", call_id=node.call["id"], reason="send_now")
                            node.cancellation.set()
                        elif policy == "wait" and not node.deferred:
                            node.deferred = True
                            self.events.emit("tool.interrupt_deferred", call_id=node.call["id"])
                if not active:
                    continue
                completed, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in completed:
                    node = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        self._finish(node, "错误：工具执行异常，未取得正常结果。")
                        raise RuntimeError(f"工具执行异常（{type(exc).__name__}）。") from None
                    self._finish(node, result)
        finally:
            # 不遗留能继续修改状态的 Python 线程；wait 工具由宿主保证有界。
            for node in active.values():
                node.cancellation.set()
            for future, node in active.items():
                try:
                    result = future.result()
                except Exception:
                    result = "错误：工具执行中止。"
                if node.result is None:
                    self._finish(node, result)

    def run(self, calls):
        if not isinstance(calls, list):
            raise RuntimeError("工具请求必须是列表。")
        ids = [call.get("id") if isinstance(call, dict) else None for call in calls]
        if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(set(ids)) != len(ids):
            raise RuntimeError("工具请求 id 缺失或重复，停止运行。")
        self.nodes = [Invocation(call, self.registry.prepare(call)) for call in calls]
        for node in self.nodes:
            prepared = node.prepared
            self.events.emit("tool.ready", call_id=node.call["id"], boundary="response_complete",
                             concurrency=prepared.tool.concurrency if not isinstance(prepared, str) else "exclusive",
                             interrupt=prepared.tool.interrupt if not isinstance(prepared, str) else "wait")
        previous_wait = self.jobs.on_wait if self.jobs is not None else None
        if self.jobs is not None:
            self.jobs.on_wait = self._pump_controls
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="agent-tool") as pool:
                index = 0
                while index < len(self.nodes):
                    node = self.nodes[index]
                    if node.result is not None:
                        index += 1
                        continue
                    if not isinstance(node.prepared, str) and node.prepared.tool.concurrency == "shared":
                        group = []
                        while index < len(self.nodes):
                            candidate = self.nodes[index]
                            if isinstance(candidate.prepared, str) or candidate.prepared.tool.concurrency != "shared":
                                break
                            group.append(candidate)
                            index += 1
                        self._shared(group, pool)
                    else:
                        self.waiting = index
                        try:
                            self._one(node)
                        finally:
                            self.waiting = None
                        index += 1
        finally:
            if self.jobs is not None:
                self.jobs.on_wait = previous_wait
        return [node.result for node in self.nodes]
