"""AI 准备的 API 接线；不包含 Agent 循环或工具执行。"""

import json
import http.client
import os
import multiprocessing
import signal
import urllib.error
import urllib.request
from pathlib import Path

from control import check_control

MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"


def _api_key():
    """优先读取环境变量；本机练习复用已有 DeepSeek 配置。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        env = json.loads(settings_path.read_text())["env"]
        if env.get("ANTHROPIC_BASE_URL", "").rstrip("/") != (
            "https://api.deepseek.com/anthropic"
        ):
            raise ValueError("现有配置不是 DeepSeek 官方地址")
        key = env["ANTHROPIC_AUTH_TOKEN"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("凭据为空")
        return key
    except (OSError, ValueError, KeyError):
        raise RuntimeError("未取得 DeepSeek 凭据，请配置 DEEPSEEK_API_KEY") from None


def _sse_data(response):
    """HTTPResponse 按行缓冲；SSE 空行分隔事件，忽略心跳和其他字段。"""
    parts = []
    for raw in response:
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise RuntimeError("流式响应不是有效 UTF-8。") from None
        if not line:
            if parts:
                yield "\n".join(parts)
                parts = []
        elif line.startswith("data:"):
            parts.append(line[5:].removeprefix(" "))
    if parts:
        yield "\n".join(parts)


def _stream_message(response, on_event):
    """收集文字和按 index 交错的工具参数；只有完整终止的流才返回消息。"""
    text, calls = [], {}
    finish_reason, done = None, False

    def emit(kind, **data):
        if on_event is not None:
            on_event({"type": kind, **data})

    for data in _sse_data(response):
        if data.strip() == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            raise RuntimeError("流式事件不是有效 JSON。") from None
        if not isinstance(chunk, dict) or "error" in chunk:
            raise RuntimeError("流式 API 返回异常事件。")
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise RuntimeError("流式事件缺少 choices。")
        if chunk.get("usage") is not None:
            emit("usage", usage=chunk["usage"])
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise RuntimeError("当前只支持单个流式 choice。")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise RuntimeError("流式事件缺少 delta。")
            if delta.get("role") not in (None, "assistant"):
                raise RuntimeError("流式响应角色不是 assistant。")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise RuntimeError("流式正文必须是字符串。")
            fragments = delta.get("tool_calls") or []
            if not isinstance(fragments, list):
                raise RuntimeError("流式工具片段必须是列表。")
            if finish_reason is not None and (content or fragments):
                raise RuntimeError("模型结束后仍返回了内容片段。")
            if content:
                text.append(content)
                emit("text.delta", text=content)
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    raise RuntimeError("工具片段不是对象。")
                index = fragment.get("index")
                if type(index) is not int or index < 0:
                    raise RuntimeError("工具片段缺少有效 index。")
                if fragment.get("type") not in (None, "function"):
                    raise RuntimeError("不支持此工具片段类型。")
                function = fragment.get("function") or {}
                if not isinstance(function, dict):
                    raise RuntimeError("工具片段缺少 function 对象。")
                pieces = (fragment.get("id") or "", function.get("name") or "", function.get("arguments") or "")
                if any(not isinstance(piece, str) for piece in pieces):
                    raise RuntimeError("工具片段字段必须是字符串。")
                call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                call["id"] += pieces[0]
                call["function"]["name"] += pieces[1]
                call["function"]["arguments"] += pieces[2]
                emit("tool.delta", index=index, id=pieces[0], name=pieces[1], arguments=pieces[2])
            reason = choice.get("finish_reason")
            if reason is not None:
                if finish_reason is not None:
                    raise RuntimeError("流式响应重复结束。")
                finish_reason = reason
    if not done or finish_reason is None:
        raise RuntimeError("模型流中断：缺少完整结束标记，已显示片段不能视为完整响应。")
    if finish_reason not in ("stop", "tool_calls"):
        raise RuntimeError(f"模型响应未正常完成：{finish_reason}")
    if calls and finish_reason != "tool_calls":
        raise RuntimeError("工具请求与流式结束原因不一致。")
    if finish_reason == "tool_calls" and not calls:
        raise RuntimeError("模型声明了工具请求但未返回工具。")
    if set(calls) != set(range(len(calls))):
        raise RuntimeError("流式工具请求缺少部分 index。")
    ordered = [calls[index] for index in sorted(calls)]
    ids = [call["id"] for call in ordered]
    if any(not call["id"] or not call["function"]["name"] for call in ordered) or len(set(ids)) != len(ids):
        raise RuntimeError("流式工具请求缺少名称、id 或 id 重复。")
    message = {"role": "assistant", "content": "".join(text) or None}
    if ordered:
        message["tool_calls"] = ordered
    return message


def _chat_direct(messages, tools=None, *, stream=False, on_event=None):
    """发送一次请求，返回 assistant 消息字典；调用者负责维护历史。"""
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": stream,
        "thinking": {"type": "disabled"},
        "max_tokens": 2048,
    }
    if tools:
        body["tools"] = tools
    if stream:
        body["stream_options"] = {"include_usage": True}
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            if stream:
                return _stream_message(response, on_event)
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"DeepSeek API 返回 HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        detail = type(reason).__name__
        if getattr(reason, "errno", None) is not None:
            detail += f", errno={reason.errno}"
        raise RuntimeError(f"DeepSeek API 连接或读取失败（{detail}）；请检查网络、代理或执行环境限制") from None
    choice = result["choices"][0]
    if choice["finish_reason"] not in ("stop", "tool_calls"):
        raise RuntimeError(f"模型响应未正常完成：{choice['finish_reason']}")
    return choice["message"]


def _chat_worker(connection, messages, tools, stream):
    # Ctrl+C 由父进程负责清理；凭据只在工作进程内部读取，不经过管道。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        def forward(event):
            connection.send(("event", event))
        message = _chat_direct(messages, tools, stream=stream, on_event=forward)
        connection.send(("result", message))
    except RuntimeError as exc:
        connection.send(("error", str(exc)))
    finally:
        connection.close()


def _process_request(target, args, *, on_event, control):
    """网络读取可阻塞；父进程每 50ms 检查控制，取消时终止并回收请求进程。"""
    check_control(control)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=target, args=(sender, *args), daemon=True)
    try:
        worker.start()
        sender.close()
        while True:
            check_control(control)
            if not receiver.poll(0.05):
                if not worker.is_alive():
                    raise RuntimeError("模型请求进程意外退出，未取得完整响应。")
                continue
            try:
                kind, value = receiver.recv()
            except EOFError:
                raise RuntimeError("模型请求进程连接关闭，未取得完整响应。") from None
            if kind == "event":
                if on_event is not None:
                    on_event(value)
            elif kind == "result":
                check_control(control)
                return value
            elif kind == "error":
                raise RuntimeError(value)
            else:
                raise RuntimeError("模型请求进程返回了未知事件。")
    finally:
        sender.close()
        receiver.close()
        if worker.pid is not None:
            worker.join(timeout=0.1)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
            if worker.is_alive():
                worker.kill()
                worker.join()
            worker.close()


def chat(messages, tools=None, *, stream=False, on_event=None, control=None):
    """控制入口启用时隔离网络请求；无控制时保持原同步调用方式。"""
    if control is None:
        return _chat_direct(messages, tools, stream=stream, on_event=on_event)
    return _process_request(_chat_worker, (messages, tools, stream),
                            on_event=on_event, control=control)
