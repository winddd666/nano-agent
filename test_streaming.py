"""SSE 协议、完整工具参数、交付前评审及增量日志的必要验证。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from agent import run_agent
from events import EventSink, JsonlEvents
from model import _stream_message, chat
from test_agent import assistant, request
from tool_registry import Parameter, Tool, ToolRegistry


def chunk(delta=None, finish=None, usage=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return value


def wire(*chunks, done=True):
    data = ": heartbeat\r\n\r\n" + "".join(
        "data: " + json.dumps(c, ensure_ascii=False) + "\r\n\r\n" for c in chunks
    )
    if done:
        data += "data: [DONE]\r\n\r\n"
    return io.BytesIO(data.encode("utf-8"))


def tool_delta(index, *, call_id=None, name=None, arguments=""):
    value = {"index": index, "function": {"arguments": arguments}}
    if call_id is not None:
        value.update(id=call_id, type="function")
    if name is not None:
        value["function"]["name"] = name
    return value


class StreamingTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_text_arrives_before_stream_finishes_and_usage_is_preserved(self):
        events = []
        response = wire(chunk({"role": "assistant", "content": "中"}),
                        chunk({"content": "文"}), chunk(finish="stop", usage={"prompt_tokens": 12}))

        def observe(event):
            self.assertFalse(response.closed)
            if event["type"] == "text.delta":
                self.assertLess(response.tell(), len(response.getvalue()))
            events.append(event)

        with patch("model._api_key", return_value="test-key"), patch("model.urllib.request.urlopen", return_value=response) as api:
            message = chat([{"role": "user", "content": "你好"}], stream=True, on_event=observe)
        self.assertEqual(message, {"role": "assistant", "content": "中文"})
        self.assertEqual([e["text"] for e in events if e["type"] == "text.delta"], ["中", "文"])
        self.assertEqual(events[-1]["usage"], {"prompt_tokens": 12})
        body = json.loads(api.call_args.args[0].data)
        self.assertTrue(body["stream"])
        self.assertTrue(body["stream_options"]["include_usage"])

    def test_interleaved_tool_arguments_are_assembled_by_index(self):
        response = wire(
            chunk({"tool_calls": [tool_delta(0, call_id="a", name="read_", arguments='{"name":"通')]}),
            chunk({"tool_calls": [tool_delta(1, call_id="b", name="list_files", arguments="{")]}),
            chunk({"tool_calls": [tool_delta(0, name="file", arguments='知.txt"}'), tool_delta(1, arguments="}")]}),
            chunk(finish="tool_calls"),
        )
        message = _stream_message(response, None)
        self.assertEqual(message["tool_calls"], [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"name":"通知.txt"}'}},
            request("b", "list_files", {}),
        ])

    def test_multiline_sse_data_and_usage_only_trailer(self):
        raw = b'data: {"choices":\ndata: [{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        raw += b'data: {"choices":[],"usage":{"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        events = []
        message = _stream_message(io.BytesIO(raw), events.append)
        self.assertEqual(message["content"], "ok")
        self.assertEqual(events[-1]["usage"]["completion_tokens"], 1)

    def test_incomplete_or_abnormal_streams_never_return_a_complete_message(self):
        cases = [
            wire(chunk({"content": "片段"}), done=False),
            wire(chunk({"content": "片段"}), chunk(finish="stop"), done=False),
            wire(chunk({"content": "片段"})),
            wire(chunk({"content": "片段"}), chunk(finish="length")),
            wire(chunk({"tool_calls": [tool_delta(1, call_id="a", name="list_files", arguments="{}")]}), chunk(finish="tool_calls")),
            wire(chunk({"tool_calls": [tool_delta(0, call_id="a", name="list_files", arguments="{}")]}), chunk(finish="stop")),
            io.BytesIO(b'data: {invalid}\n\ndata: [DONE]\n\n'),
        ]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                _stream_message(response, None)

    def test_tool_handler_runs_only_after_complete_stream_and_parameter_validation(self):
        handler = Mock(return_value="查到了")
        registry = ToolRegistry([Tool("lookup", "查询", {"query": Parameter("查询内容")}, handler)])
        first = wire(
            chunk({"tool_calls": [tool_delta(0, call_id="q", name="lookup", arguments='{"query":')]}),
            chunk({"tool_calls": [tool_delta(0, arguments='"课程"}')]}), chunk(finish="tool_calls"),
        )
        second = wire(chunk({"content": "答案"}), chunk(finish="stop"))
        events = []

        def observe(event):
            if event["type"] in ("model.tool.delta", "tool.started"):
                handler.assert_not_called()
            if event["type"] == "model.completed" and event["request"] == 1:
                handler.assert_not_called()
            events.append(event)

        with patch("agent.REGISTRY", registry), patch("model._api_key", return_value="test-key"):
            with patch("model.urllib.request.urlopen", side_effect=[first, second]):
                report = run_agent("查询课程", stream=True, review=False, on_event=observe)
        handler.assert_called_once_with(query="课程")
        self.assertEqual(report["answer"], "答案")
        kinds = [e["type"] for e in events]
        self.assertLess(kinds.index("model.completed"), kinds.index("tool.started"))

    def test_truncated_tool_stream_does_not_execute_or_enter_canonical_history(self):
        response = wire(chunk({"tool_calls": [tool_delta(0, call_id="q", name="list_files", arguments="{}")]}), done=False)
        with patch("model._api_key", return_value="test-key"), patch("model.urllib.request.urlopen", return_value=response):
            with patch("tool_registry.ToolRegistry.execute") as execute:
                report = run_agent("查目录", stream=True, review=False, on_event=lambda e: None)
        execute.assert_not_called()
        self.assertEqual(report["status"], "runtime_error")
        self.assertEqual([m["role"] for m in report["messages"]], ["system", "user"])
        self.assertTrue(any(e["type"] == "model.tool.delta" for e in report["events"]))
        self.assertFalse(any(e["type"] in ("candidate.ready", "answer", "tool.started") for e in report["events"]))

    def test_parseable_tool_json_is_not_a_per_tool_completion_boundary(self):
        handler = Mock(return_value="真实结果")
        registry = ToolRegistry([Tool("lookup", "查询", {}, handler, concurrency="shared")])
        first = wire(chunk({"tool_calls": [tool_delta(0, call_id="q", name="lookup", arguments="{}")]}),
                     chunk({"content": "参数看起来已完整，但响应仍在继续。"}),
                     chunk({"tool_calls": [tool_delta(0, arguments=" ")]}), chunk(finish="tool_calls"))
        second = wire(chunk({"content": "最终答案"}), chunk(finish="stop"))

        def listener(event):
            if event["type"] in ("model.tool.delta", "model.text.delta") and event["request"] == 1:
                handler.assert_not_called()

        with patch("agent.REGISTRY", registry), patch("model._api_key", return_value="test-key"):
            with patch("model.urllib.request.urlopen", side_effect=[first, second]):
                report = run_agent("查询", stream=True, review=False, on_event=listener)
        handler.assert_called_once_with()
        self.assertEqual(report["tool_start_boundary"], "response_complete")
        kinds = [e["type"] for e in report["events"]]
        self.assertLess(kinds.index("model.completed"), kinds.index("tool.ready"))

    def test_user_interrupt_keeps_partial_text_as_events_not_as_an_answer(self):
        class Interrupted(io.BytesIO):
            def __iter__(self):
                yield from wire(chunk({"content": "尚未完成"}), done=False)
                raise KeyboardInterrupt

        with patch("model._api_key", return_value="test-key"), patch("model.urllib.request.urlopen", return_value=Interrupted()):
            report = run_agent("回答问题", stream=True, on_event=lambda e: None)
        self.assertEqual(report["status"], "interrupted")
        self.assertNotIn("answer", report)
        self.assertEqual(len(report["messages"]), 2)
        self.assertTrue(any(e["type"] == "model.text.delta" for e in report["events"]))
        self.assertEqual(report["events"][-1]["type"], "run.finished")

    def test_streaming_reviewer_remains_advisory_and_scopes_share_event_order(self):
        responses = [wire(chunk({"content": "候选"}), chunk(finish="stop")),
                     wire(chunk({"content": '{"recommendation":"revise",'}),
                          chunk({"content": '"reason":"无关建议"}'}), chunk(finish="stop")),
                     wire(chunk({"content": "不采纳"}), chunk({"content": "并说明依据。"}), chunk(finish="stop"))]
        with patch("model._api_key", return_value="test-key"), patch("model.urllib.request.urlopen", side_effect=responses):
            report = run_agent("回答问题", stream=True, on_event=lambda e: None)
        self.assertEqual(report["answer"], "不采纳并说明依据。")
        self.assertEqual(report["reviews"][0]["recommendation"], "revise")
        events = report["events"]
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events)+1)))
        self.assertTrue(all(e["scope"] == "reviewer-1" for e in report["reviews"][0]["events"]))
        kinds = [e["type"] for e in events]
        self.assertLess(kinds.index("candidate.ready"), kinds.index("review.started"))
        self.assertLess(kinds.index("review.finished"), kinds.index("answer"))

    def test_jsonl_flushes_tool_intent_before_the_handler_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"events.jsonl"

            def handler():
                last = json.loads(path.read_text().splitlines()[-1])
                self.assertEqual(last["type"], "tool.started")
                self.assertEqual(last["call_id"], "q")
                return "结果"

            registry = ToolRegistry([Tool("lookup", "查询", {}, handler)])
            with JsonlEvents(path) as listener, patch("agent.REGISTRY", registry):
                with patch("agent.chat", side_effect=[assistant(request("q", "lookup", {})), assistant(content="答案")]):
                    report = run_agent("查询", review=False, on_event=listener)
            self.assertEqual(report["status"], "answered")
            self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], report["events"])

    def test_journal_failure_during_review_stops_all_further_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            class FailingFile:
                def __init__(self, file):
                    self.file = file
                def write(self, text):
                    if json.loads(text)["type"] == "tool.started":
                        raise OSError("disk full")
                    return self.file.write(text)
                def flush(self):
                    self.file.flush()
                def close(self):
                    self.file.close()

            with JsonlEvents(Path(directory)/"events.jsonl") as listener:
                listener.file = FailingFile(listener.file)
                with patch("agent.chat", return_value=assistant(content="候选")) as actor:
                    with patch("reviewer.chat", return_value=assistant(request("q", "list_files", {}))):
                        with patch("tool_registry.ToolRegistry.execute") as execute:
                            report = run_agent("查询", on_event=listener)
            execute.assert_not_called()
            self.assertEqual(actor.call_count, 1)
            self.assertEqual(report["status"], "runtime_error")
            self.assertEqual(report["reviewer_requests"], 1)
            self.assertNotIn("answer", report)


if __name__ == "__main__":
    unittest.main()
