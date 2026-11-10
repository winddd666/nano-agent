"""过程事件、即时终端展示和追加写入的 JSONL 日志。"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from control import RequestCancelled, check_control


class EventSink:
    def __init__(self, on_event=None, *, scope="actor", parent=None):
        self.events = []
        self.scope = scope
        self.parent = parent
        self.listener = ConsoleRenderer() if on_event is None else on_event
        self.started = perf_counter()
        self.observer_error = None

    def child(self, scope):
        return EventSink(scope=scope, parent=self)

    def emit(self, event_type, **data):
        if self.parent is not None:
            event = self.parent.emit(event_type, scope=self.scope, **data)
            self.events.append(event)
            return event
        event = {
            "seq": len(self.events) + 1, "time": datetime.now(timezone.utc).isoformat(),
            "elapsed": perf_counter() - self.started, "scope": self.scope,
            **deepcopy(data), "type": event_type,
        }
        self.events.append(event)
        try:
            self.listener(event)
        except RuntimeError as exc:
            self.observer_error = str(exc)
            raise
        return event


def request_model(chat, messages, tools, *, stream, events, request, control=None):
    """把一次请求的生命周期和增量输出接到统一事件通道。"""
    events.emit("model.request", request=request, stream=stream, messages=messages, tools=tools)
    events.emit("model.started", request=request, stream=stream)
    started = perf_counter()

    def forward(event):
        events.emit("model." + event["type"], request=request,
                    **{key: value for key, value in event.items() if key != "type"})

    try:
        check_control(control)
        extra = {} if control is None else {"control": control}
        if stream:
            message = chat(messages, tools=tools, stream=True, on_event=forward, **extra)
        else:
            message = chat(messages, tools=tools, **extra)
            if isinstance(message, dict) and message.get("content"):
                events.emit("model.text", request=request, text=message["content"])
        check_control(control)
        events.emit("model.completed", request=request, message=message, duration=perf_counter() - started)
        return message
    except (RuntimeError, KeyboardInterrupt) as exc:
        events.emit("model.failed", request=request,
                    error="用户中断" if isinstance(exc, KeyboardInterrupt) else str(exc),
                    cancelled=isinstance(exc, RequestCancelled))
        raise


class ConsoleRenderer:
    def __init__(self):
        self.open_text = False
        self.seen_tools = set()
        self.seen_review_text = set()

    def newline(self):
        if self.open_text:
            print(flush=True)
            self.open_text = False

    def __call__(self, event):
        kind, scope = event["type"], event["scope"]
        label = "主模型" if scope == "actor" else scope
        if kind == "model.started":
            self.newline()
            print(f"\n[{label} 请求 {event['request']}，{'流式' if event['stream'] else '非流式'}；生成中，尚未交付]", flush=True)
        elif kind in ("model.text.delta", "model.text"):
            if scope == "actor":
                print(event["text"], end="", flush=True)
                self.open_text = True
            elif (scope, event["request"]) not in self.seen_review_text:
                self.seen_review_text.add((scope, event["request"]))
                print("[评审者正在形成建议]", flush=True)
        elif kind == "model.tool.delta":
            key = (scope, event["request"], event["index"])
            if key not in self.seen_tools:
                self.seen_tools.add(key)
                self.newline()
                print(f"[{label} 工具参数接收中 #{event['index']}]", flush=True)
        elif kind in ("model.completed", "model.failed"):
            self.newline()
        elif kind == "tool.started":
            self.newline()
            print(f"[{label} 工具请求] {json.dumps(event['function'], ensure_ascii=False)}", flush=True)
        elif kind == "tool.completed":
            print(f"[{label} 工具结果]\n{event['result']}", flush=True)
        elif kind == "tool.skipped":
            print(f"[工具 {event['call_id']} 尚未启动，已按立即处理指令跳过]", flush=True)
        elif kind in ("job.started", "job.backgrounded", "job.waiting", "job.completed"):
            self.newline()
            print(f"[任务 {event['job_id']}] {event.get('status') or kind.removeprefix('job.')}"
                  f"{('，退出码 ' + str(event['exit_code'])) if event.get('exit_code') is not None else ''}", flush=True)
        elif kind == "control.handled":
            self.newline()
            print(f"[后台化指令] {event['outcome']}", flush=True)
        elif kind == "candidate.ready":
            print("[候选回答：进入交付前评审]", flush=True)
        elif kind == "input.received":
            self.newline()
            print(f"[已接收追加消息 #{event['id']}] {event['text']}", flush=True)
        elif kind == "input.applied":
            print(f"[消息 #{event['input_id']} 已生效：任务版本 {event['task_version']}，"
                  f"节点 {event['boundary']}]", flush=True)
        elif kind == "input.pending":
            print(f"[消息 #{event['input_id']} 尚未生效：运行已停止（{event['reason']}）]", flush=True)
        elif kind == "notification.applied":
            self.newline()
            print(f"[任务完成通知已进入上下文] {event['payload']['job_id']}：{event['payload']['status']}", flush=True)
        elif kind in ("output.superseded", "review.superseded"):
            self.newline()
            subject = "回答" if kind == "output.superseded" else "评审建议"
            print(f"[上下文已更新，版本 {event['task_version']} 的{subject}过时，继续处理]", flush=True)
        elif kind == "review.started":
            print(f"[{scope} 开始独立评审]", flush=True)
        elif kind in ("review.finished", "review.unavailable"):
            self.newline()
            print(f"[评审建议] {event.get('recommendation') or event['status']}："
                  f"{event.get('reason') or event.get('error') or '未取得有效建议'}", flush=True)
        elif kind == "run.finished":
            self.newline()
            if event["status"] == "answered":
                print("[主智能体已交付]", flush=True)
            else:
                print(f"[运行停止：{event['status']}] {event.get('error') or '任务未完成，已显示片段不代表交付。'}", flush=True)


class JsonlEvents:
    """每个事件先写入并 flush，再展示；写入失败阻止后续行动。"""
    def __init__(self, path):
        self.path = Path(path)
        self.renderer = ConsoleRenderer()
        self.failed = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("x", encoding="utf-8")
        return self

    def __call__(self, event):
        if not self.failed:
            try:
                self.file.write(json.dumps(event, ensure_ascii=False) + "\n")
                self.file.flush()
            except OSError:
                self.failed = True
                raise RuntimeError("事件日志写入失败，停止后续执行。") from None
        self.renderer(event)

    def __exit__(self, *args):
        self.file.close()
